
# -*- coding: utf-8 -*-
"""
Jira Defect Creator + Zephyr Updater (Cloud)
- Creates a Jira defect with ADF description
- Uploads attachments selected in the UI
- Finds latest Zephyr execution for the test; links defect; fails step & execution

Requirements:
  pip install streamlit requests

Secrets required in .streamlit/secrets.toml:
  JIRA_EMAIL, JIRA_API_TOKEN, ATLASSIAN_ACCOUNT_ID, ZEPHYR_ACCESS_KEY, ZEPHYR_SECRET_KEY
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import re

import streamlit as st
import requests
from requests.auth import HTTPBasicAuth

# ============================================================
# CONFIGURATION — EDIT TO YOUR ENVIRONMENT
# ============================================================
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"

# Issue type candidates (first existing one will be used)
ISSUE_TYPE_CANDIDATES = ["Defect", "Bug"]

# Known single-select custom field IDs in your Jira project
TEST_PHASE_FIELD_ID = "customfield_10245"  # "Test Phase"
SEVERITY_FIELD_ID   = "customfield_10260"  # "Severity"

# Zephyr Squad Cloud base URL (Cloud tenants)
ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

# Some Cloud tenants don’t expose step-result APIs consistently.
# Set to True if your tenant supports GET/PUT of step results via API.
ENABLE_STEP_UPDATE = True

# ============================================================
# STREAMLIT UI
# ============================================================
st.set_page_config(page_title="Create Defect + Update Zephyr", layout="centered")
st.title("🐞 Create Jira Defect + 📊 Update Zephyr Execution")

with st.expander("🔧 Settings", expanded=True):
    test_ticket = st.text_input("Test Ticket Key", value="", placeholder="e.g., CT-213125")
    failed_step_num = st.number_input("Failed Step Number", min_value=1, value=3, step=1)
    severity = st.selectbox("Severity", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
    priority = st.selectbox("Priority", ["Critical", "Major", "Medium", "Minor"])
    test_phase = st.selectbox("Test Phase", ["SIT", "FAT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"])
    uploaded_files = st.file_uploader("📎 Attach Evidence (screenshots, logs)", accept_multiple_files=True)

st.caption("NOTE: Jira Cloud requires **ADF** for rich text fields. This app builds the defect description in ADF automatically. "
           "Zephyr operations use JWT + zapiAccessKey per request and fall back to **ZQL** if needed.")


# ============================================================
# AUTH HELPERS
# ============================================================
def get_auth():
    try:
        return HTTPBasicAuth(st.secrets["JIRA_EMAIL"], st.secrets["JIRA_API_TOKEN"])
    except KeyError as e:
        st.error(f"Missing secret: {e}. Add credentials in Streamlit Secrets.")
        st.stop()

def headers_json():
    return {"Accept": "application/json", "Content-Type": "application/json"}


# ============================================================
# JIRA HELPERS
# ============================================================
def jira_issue_id_from_key(issue_key, auth):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    return r.json()["id"]

def jira_project_id_from_key(project_key, auth):
    url = f"{JIRA_BASE_URL}/rest/api/3/project/{project_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    return int(r.json()["id"])

def get_issue_type_id(auth):
    # Resolve issue type id, using candidates
    url_it = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes"
    r = requests.get(url_it, headers={"Accept":"application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    types = r.json().get("issueTypes", []) or []
    name_to_id = {t.get("name"): t.get("id") for t in types if t.get("id")}
    for candidate in ISSUE_TYPE_CANDIDATES:
        if candidate in name_to_id:
            return name_to_id[candidate]
    # fallback to first available
    return (types or [{}])[0].get("id")

def get_field_option_id_from_createmeta(auth, issue_type_id, field_id, chosen_label):
    """Resolves option id for a single-select custom field on the create screen."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes/{issue_type_id}"
    r = requests.get(url, headers={"Accept":"application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    meta = r.json()
    for f in meta.get("fields", []):
        if f.get("fieldId") == field_id:
            for opt in (f.get("allowedValues") or []):
                if (opt.get("value") or opt.get("name")) == chosen_label:
                    return opt.get("id")
    return None


# ============================================================
# ZEPHYR JWT & REQUEST HELPERS (Cloud)
# Docs: base URL, JWT + zapiAccessKey headers, and per-request JWT.  [2](https://support.smartbear.com/zephyr-squad-cloud-v1/docs/en/zephyr-squad-cloud-rest-api.html)
# ============================================================
def build_zephyr_jwt(method, relative_path, query_params=None, expires_in=360):
    method = method.upper()
    query_params = query_params or {}
    canonical_qs = urllib.parse.urlencode(sorted(query_params.items()), doseq=True)
    canonical_req = f"{method}&{relative_path}&{canonical_qs}"
    qsh = hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()
    now = int(time.time())
    payload = {
        "sub": st.secrets["ATLASSIAN_ACCOUNT_ID"],
        "qsh": qsh,
        "iss": st.secrets["ZEPHYR_ACCESS_KEY"],
        "exp": now + expires_in,
        "iat": now
    }
    header = {"typ": "JWT", "alg": "HS256"}

    def b64(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj, separators=(",", ":")).encode()
        ).rstrip(b"=")

    signing_input = b".".join([b64(header), b64(payload)])
    signature = base64.urlsafe_b64encode(
        hmac.new(st.secrets["ZEPHYR_SECRET_KEY"].encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return signing_input.decode() + "." + signature.decode()

def zephyr_request(method, relative_path, query_params=None, json_body=None):
    method = method.upper()
    query_params = query_params or {}
    jwt = build_zephyr_jwt(method, relative_path, query_params)
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": st.secrets["ZEPHYR_ACCESS_KEY"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.request(method, url, headers=headers, params=query_params, json=json_body, timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr {method} {relative_path} failed {r.status_code}: {r.text[:400]}")
    if r.text.strip():
        try:
            return r.json()
        except Exception:
            return r.text
    return None

def zephyr_get(relative_path, query_params=None):
    return zephyr_request("GET", relative_path, query_params=query_params)

def zephyr_post(relative_path, json_body=None, query_params=None):
    return zephyr_request("POST", relative_path, query_params=query_params, json_body=json_body)

def zephyr_put(relative_path, json_body=None, query_params=None):
    return zephyr_request("PUT", relative_path, query_params=query_params, json_body=json_body)


# ============================================================
# ADF HELPERS (Jira Cloud description must be ADF) [1](https://developer.atlassian.com/cloud/jira/platform/rest/v3/)
# ============================================================
def adf_text(text, marks=None):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node

def adf_paragraph(text):
    return {"type": "paragraph", "content": [adf_text(text)]}

def adf_heading(text, level=2):
    return {"type": "heading", "attrs": {"level": level}, "content": [adf_text(text)]}

def adf_ordered_list(items):
    return {
        "type": "orderedList",
        "content": [{"type": "listItem", "content": [adf_paragraph(i)]} for i in items]
    }

def build_adf_description(test_key, failed_step_num, test_phase, steps, expected_text, actual_text, evidence_names):
    """
    Build a clean ADF description with sections:
      Issue Description | Steps to reproduce | Expected Results | Actual results | Evidences
    """
    content = []

    # Issue Description
    content.append(adf_heading("Issue Description"))
    content.append(adf_paragraph(
        f"During {test_phase} testing, Step {failed_step_num} for Test {test_key} fails to proceed as expected, preventing completion of the workflow."
    ))

    # Steps to reproduce
    content.append(adf_heading("Steps to reproduce"))
    if steps:
        # Bold quoted fragments if present; otherwise just list the steps
        content.append(adf_ordered_list(steps))
    else:
        content.append(adf_paragraph("(No detailed steps available)"))

    # Expected Results
    content.append(adf_heading("Expected Results"))
    content.append(adf_paragraph(expected_text or "System should accept the selection and continue without errors."))

    # Actual results
    content.append(adf_heading("Actual results"))
    content.append(adf_paragraph(actual_text or f"System fails at Step {failed_step_num}. See attachments for details."))

    # Evidences
    content.append(adf_heading("Evidences"))
    if evidence_names:
        for name in evidence_names:
            content.append(adf_paragraph(f"- {name}"))
    else:
        content.append(adf_paragraph("See attachments."))

    return {"type": "doc", "version": 1, "content": content}


# ============================================================
# ZEPHYR: STEPS + EXECUTION + LINK/FAIL
# ============================================================
def get_zephyr_steps_and_expected(jira_test_key, auth):
    """
    Retrieve Zephyr test steps and expected results for a Jira Test issue.
    """
    issue_id = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)
    rel = f"/public/rest/api/1.0/teststep/{issue_id}"
    rows = zephyr_get(rel, query_params={"projectId": project_id})
    steps, expected = [], []
    for s in sorted(rows, key=lambda x: x.get("orderId", 0)):
        step_txt = (s.get("step") or "").strip()
        exp_txt  = (s.get("result") or "").strip()
        if step_txt:
            steps.append(step_txt)
        if exp_txt:
            expected.append(exp_txt)
    return steps, expected

def find_latest_execution_id(jira_test_key, auth):
    """
    Strategy:
      1) Try Cloud listing: GET /public/rest/api/1.0/executions?issueId=&projectId=
      2) Fallback: ZQL search: GET /public/rest/api/1.0/zql/executeSearch (ORDER BY executionDate DESC)
    """
    issue_id = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)

    # 1) listing
    try:
        rel = "/public/rest/api/1.0/executions"
        params = {"issueId": issue_id, "projectId": project_id, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        execs = (data.get("executions") or (data.get("searchResult") or {}).get("executions") or [])
        if execs:
            execs_sorted = sorted(execs, key=lambda e: e.get("orderId", 0), reverse=True)
            top = execs_sorted[0]
            return str(top.get("id")) if top.get("id") is not None else None
    except Exception:
        pass

    # 2) ZQL fallback (Cloud ZQL docs / fields list)  [3](https://support.smartbear.com/zephyr-squad-cloud-v1/docs/en/execute-tests/zql-reference.html)
    try:
        rel = "/public/rest/api/1.0/zql/executeSearch"
        zql = f'issue = "{jira_test_key}" ORDER BY executionDate DESC'
        params = {"zqlQuery": zql, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        execs = (data.get("searchResult") or {}).get("executions") or []
        if execs:
            execs_sorted = sorted(execs, key=lambda e: e.get("orderId", 0), reverse=True)
            top = execs_sorted[0]
            return str(top.get("id")) if top.get("id") is not None else None
    except Exception:
        pass

    return None

def link_defect_to_execution(execution_id, defect_issue_key, auth):
    defect_issue_id = jira_issue_id_from_key(defect_issue_key, auth)
    rel = f"/public/rest/api/1.0/execution/{execution_id}/defects"
    body = {"issueId": defect_issue_id}
    zephyr_post(rel, json_body=body)

def fail_zephyr_step(execution_id, step_num):
    rel_steps = f"/public/rest/api/1.0/execution/{execution_id}/steps"
    steps = zephyr_get(rel_steps)
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("No step results returned for this execution (step API unsupported on this tenant?).")
    steps_sorted = sorted(steps, key=lambda x: x.get("orderId", 0))
    idx = max(1, int(step_num)) - 1
    if idx >= len(steps_sorted):
        idx = len(steps_sorted) - 1
    step_result_id = steps_sorted[idx].get("id") or steps_sorted[idx].get("stepResultId")
    if not step_result_id:
        raise RuntimeError("Couldn't resolve stepResultId from Zephyr response.")
    rel_update = f"/public/rest/api/1.0/execution/{execution_id}/stepResult/{step_result_id}"
    body = {"status": {"id": 2}}  # Fail
    zephyr_put(rel_update, json_body=body)

def fail_execution(execution_id):
    rel = f"/public/rest/api/1.0/execution/{execution_id}"
    body = {"status": {"id": 2}}  # Fail
    zephyr_put(rel, json_body=body)


# ============================================================
# ACTION: CREATE DEFECT + ATTACH + LINK + FAIL STEP + FAIL EXEC
# ============================================================
def create_defect_and_update_zephyr():
    if not test_ticket.strip():
        st.error("Enter the Test Ticket key.")
        st.stop()

    auth = get_auth()

    # ---- Resolve issue type
    try:
        issue_type_id = get_issue_type_id(auth)
    except Exception as e:
        st.error(f"Failed to fetch issue types: {e}")
        st.stop()

    # ---- Resolve option IDs for Severity & Test Phase using createmeta (safer than plain label)
    severity_id = None
    phase_id = None
    try:
        severity_id = get_field_option_id_from_createmeta(auth, issue_type_id, SEVERITY_FIELD_ID, severity)
        phase_id = get_field_option_id_from_createmeta(auth, issue_type_id, TEST_PHASE_FIELD_ID, test_phase)
        if not severity_id or not phase_id:
            st.warning("Could not resolve option IDs from createmeta; falling back to label values.")
    except Exception:
        st.warning("Createmeta resolution failed; falling back to label values.")

    # ---- (Optional) fetch steps & expected from Zephyr to enrich ADF description
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip(), auth)
    except Exception:
        steps_list, expected_from_zephyr = [], []

    # Compose expected/actual
    expected_text = expected_from_zephyr[0] if expected_from_zephyr else "System should accept the selection and continue without errors."
    actual_text = f"System fails to proceed at Step {failed_step_num}. See attachments for details."

    # Evidence names for the ADF "Evidences" section (file names only)
    evidence_names = [f.name for f in (uploaded_files or [])]

    # Build ADF description (Jira Cloud requires ADF for description)  [1](https://developer.atlassian.com/cloud/jira/platform/rest/v3/)
    description_adf = build_adf_description(
        test_key=test_ticket.strip(),
        failed_step_num=int(failed_step_num),
        test_phase=test_phase,
        steps=steps_list,
        expected_text=expected_text,
        actual_text=actual_text,
        evidence_names=evidence_names
    )

    # ---- Create defect
    summary = f"{test_ticket.strip()}: Step {failed_step_num} failed during {test_phase}"

    fields = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"id": issue_type_id},
        "summary": summary,
        "description": description_adf,
        "priority": {"name": priority}
    }

    # Add custom fields either by id or label fallback
    if severity_id:
        fields[SEVERITY_FIELD_ID] = {"id": severity_id}
    else:
        fields[SEVERITY_FIELD_ID] = {"value": severity}  # fallback

    if phase_id:
        fields[TEST_PHASE_FIELD_ID] = {"id": phase_id}
    else:
        fields[TEST_PHASE_FIELD_ID] = {"value": test_phase}  # fallback

    create_payload = {"fields": fields}

    create_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=create_payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )

    if create_resp.status_code != 201:
        st.error(f"❌ Defect creation failed: {create_resp.text}")
        st.stop()

    issue_key = create_resp.json()["key"]
    st.success(f"✅ Defect created: {issue_key}")

    # ---- Upload attachments (UI-chosen)
    if uploaded_files:
        for file in uploaded_files:
            attach_headers = {"X-Atlassian-Token": "no-check", "Accept": "application/json"}
            files = {"file": (file.name, file.getvalue())}
            attach_resp = requests.post(
                f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments",
                headers=attach_headers,
                files=files,
                auth=auth,
                timeout=60
            )
            if attach_resp.status_code not in (200, 201):
                st.warning(f"Attachment '{file.name}' failed: {attach_resp.status_code} {attach_resp.text[:180]}")
        st.success("📎 Attachments uploaded.")

    # ---- Zephyr: find latest execution, link defect, fail step & execution
    try:
        execution_id = find_latest_execution_id(test_ticket.strip(), auth)
        if not execution_id:
            st.warning("No Zephyr execution found for this Test.")
        else:
            # Link defect to execution (shows in Zephyr Defects section)
            try:
                link_defect_to_execution(execution_id, issue_key, auth)
                st.success("🔗 Defect linked to Zephyr execution (Defects section).")
            except Exception as e:
                st.warning(f"Failed to link defect to Zephyr execution: {e}")

            # Fail specific step (best-effort)
            try:
                if ENABLE_STEP_UPDATE:
                    fail_zephyr_step(execution_id, int(failed_step_num))
                    st.success(f"❗ Step {failed_step_num} marked as FAILED in Zephyr.")
                else:
                    st.info("Step update skipped (ENABLE_STEP_UPDATE = False).")
            except Exception as e:
                st.warning(f"Could not update Zephyr failed step: {e}")

            # Fail the whole execution
            try:
                fail_execution(execution_id)
                st.success("🟥 Zephyr execution status set to FAIL.")
            except Exception as e:
                st.warning(f"Failed to update Zephyr execution status: {e}")

    except Exception as e:
        st.warning(f"Zephyr operations failed: {e}")

    # Link out to the new defect
    st.link_button("Open Defect in Jira", f"{JIRA_BASE_URL}/browse/{issue_key}")


# ============================================================
# RUN ACTION
# ============================================================
if st.button("🚀 Create Defect + Update Zephyr"):
    create_defect_and_update_zephyr()
