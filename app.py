
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
# CONFIGURATION
# ============================================================
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"
ISSUE_TYPE_CANDIDATES = ["Defect", "Bug"]

TEST_PHASE_FIELD_ID = "customfield_10245"  # Test Phase
SEVERITY_FIELD_ID = "customfield_10260"    # Severity

CUST_TECH_PORTFOLIO_NAME = "Cust Tech Portfolio"
CUST_TECH_PRODUCT_NAME = "Cust Tech Product"
CUST_TECH_DELIVERY_TEAM_NAME = "Cust Tech Delivery Team"

EXPECTED_RESULTS_NAME = "Expected Results"
ACTUAL_RESULTS_NAME = "Actual Results"
IMPACT_NAME = "Impact"

ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

# Feature flag for step updates
ENABLE_STEP_UPDATE = True  # Set False if your tenant doesn't support step updates

# ============================================================
# AUTH HELPERS
# ============================================================
def get_auth():
    return HTTPBasicAuth(st.secrets["JIRA_EMAIL"], st.secrets["JIRA_API_TOKEN"])

def headers_json():
    return {"Accept": "application/json", "Content-Type": "application/json"}

# ============================================================
# JIRA HELPERS
# ============================================================
def jira_issue_id_from_key(issue_key, auth):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth)
    r.raise_for_status()
    return r.json()["id"]

def jira_project_id_from_key(project_key, auth):
    url = f"{JIRA_BASE_URL}/rest/api/3/project/{project_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth)
    r.raise_for_status()
    return int(r.json()["id"])

# ============================================================
# ZEPHYR JWT & REQUEST HELPERS
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
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")

    signing_input = b".".join([b64(header), b64(payload)])
    signature = base64.urlsafe_b64encode(
        hmac.new(st.secrets["ZEPHYR_SECRET_KEY"].encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return signing_input.decode() + "." + signature.decode()

def zephyr_request(method, relative_path, query_params=None, json_body=None):
    jwt = build_zephyr_jwt(method, relative_path, query_params or {})
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": st.secrets["ZEPHYR_ACCESS_KEY"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.request(method, url, headers=headers, params=query_params, json=json_body)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr {method} {relative_path} failed {r.status_code}: {r.text[:400]}")
    return r.json() if r.text.strip() else None

def zephyr_get(path, query_params=None):
    return zephyr_request("GET", path, query_params=query_params)

def zephyr_post(path, json_body=None, query_params=None):
    return zephyr_request("POST", path, query_params=query_params, json_body=json_body)

def zephyr_put(path, json_body=None, query_params=None):
    return zephyr_request("PUT", path, query_params=query_params, json_body=json_body)

# ============================================================
# ZEPHYR EXECUTION HELPERS
# ============================================================
def find_latest_execution_id(jira_test_key, auth):
    issue_id = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)

    # Try direct executions listing
    try:
        rel = "/public/rest/api/1.0/executions"
        params = {"issueId": issue_id, "projectId": project_id, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        execs = (data.get("executions") or (data.get("searchResult") or {}).get("executions") or [])
        if execs:
            execs_sorted = sorted(execs, key=lambda e: e.get("orderId", 0), reverse=True)
            return str(execs_sorted[0].get("id"))
    except Exception:
        pass

    # Fallback: ZQL search
    try:
        rel = "/public/rest/api/1.0/zql/executeSearch"
        zql = f'issue = "{jira_test_key}" ORDER BY executionDate DESC'
        params = {"zqlQuery": zql, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        execs = (data.get("searchResult") or {}).get("executions") or []
        if execs:
            execs_sorted = sorted(execs, key=lambda e: e.get("orderId", 0), reverse=True)
            return str(execs_sorted[0].get("id"))
    except Exception:
        pass

    return None

def fail_execution(execution_id):
    rel = f"/public/rest/api/1.0/execution/{execution_id}"
    body = {"status": {"id": 2}}  # 2 = Fail
    zephyr_put(rel, json_body=body)

def fail_zephyr_step(execution_id, step_num):
    rel_steps = f"/public/rest/api/1.0/execution/{execution_id}/steps"
    steps = zephyr_get(rel_steps)
    if not steps:
        raise RuntimeError("No step results returned.")
    steps_sorted = sorted(steps, key=lambda x: x.get("orderId", 0))
    idx = max(1, step_num) - 1
    step_result_id = steps_sorted[idx].get("id") or steps_sorted[idx].get("stepResultId")
    rel_update = f"/public/rest/api/1.0/execution/{execution_id}/stepResult/{step_result_id}"
    body = {"status": {"id": 2}}  # Fail
    zephyr_put(rel_update, json_body=body)

def link_defect_to_execution(execution_id, defect_issue_key, auth):
    defect_issue_id = jira_issue_id_from_key(defect_issue_key, auth)
    rel = f"/public/rest/api/1.0/execution/{execution_id}/defects"
    body = {"issueId": defect_issue_id}
    zephyr_post(rel, json_body=body)

# ============================================================
# STREAMLIT UI
# ============================================================
st.title("🐞 Create Jira Defect and Update Zephyr Execution")
test_ticket = st.text_input("Test Ticket Key (e.g., CT-213125)")
failed_step_num = st.number_input("Failed Step Number", min_value=1, value=3)
severity = st.selectbox("Severity", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
priority = st.selectbox("Priority", ["Critical", "Major", "Medium", "Minor"])
test_phase = st.selectbox("Test Phase", ["SIT", "FAT", "Regression", "Production"])

if st.button("🚀 Create Defect and Update Zephyr"):
    auth = get_auth()

    # Create defect (simplified for demo)
    summary = f"Issue in {test_ticket}: Step {failed_step_num} failed"
    description = f"During {test_phase} testing, step {failed_step_num} failed. See attachments for details."
    payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Bug"},
            "priority": {"name": priority},
            SEVERITY_FIELD_ID: {"value": severity},
            TEST_PHASE_FIELD_ID: {"value": test_phase}
        }
    }
    resp = requests.post(f"{JIRA_BASE_URL}/rest/api/3/issue", json=payload, headers=headers_json(), auth=auth)
    if resp.status_code != 201:
        st.error(f"Defect creation failed: {resp.text}")
    else:
        issue_key = resp.json()["key"]
        st.success(f"✅ Defect created: {issue_key}")

        # Zephyr updates
        execution_id = find_latest_execution_id(test_ticket.strip(), auth)
        if execution_id:
            try:
                link_defect_to_execution(execution_id, issue_key, auth)
                st.success("🔗 Defect linked to Zephyr execution.")
            except Exception as e:
                st.warning(f"Failed to link defect: {e}")

            if ENABLE_STEP_UPDATE:
                try:
                    fail_zephyr_step(execution_id, failed_step_num)
                    st.success(f"❗ Step {failed_step_num} marked as FAILED.")
                except Exception as e:
                    st.warning(f"Failed to update step: {e}")

            try:
                fail_execution(execution_id)
                st.success("🟥 Execution marked as FAIL.")
            except Exception as e:
                st.warning(f"Failed to update execution: {e}")
        else:
            st.warning("No Zephyr execution found for this Test.")
