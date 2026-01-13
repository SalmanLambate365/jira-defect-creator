
# app.py
# --------------------------------------------------------------------------------
# Jira Defect Creator (Create defect from Test; copy fields; Zephyr integration)
# Enhancements:
# - Correct mapping of Cust Tech Delivery Teams & Cust Tech Products
# - Parent field set to Epic linked to the Test ticket's linked Story (best-effort)
#   * Find executions and extract nested execution IDs + required identifiers# - Labels copied excluding 'JiraTestGenAI'
#   * Link defects via PUT /execution/{id}/execute with IDs (Cloud)
#   * Fail execution via PUT /execution/{id}/execute with IDs (Cloud)
#   * Step updates via /stepresult endpoints (feature-flagged)
# --------------------------------------------------------------------------------
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

# Known single-select custom field IDs in your Jira
TEST_PHASE_FIELD_ID = "customfield_10245"  # Test Phase
SEVERITY_FIELD_ID   = "customfield_10260"  # Severity

# Resolve these by NAME → ID at runtime (case-insensitive)
CUST_TECH_PORTFOLIO_NAME     = "Cust Tech Portfolio"
CUST_TECH_PRODUCT_NAME       = "Cust Tech Products"
CUST_TECH_DELIVERY_TEAM_NAME = "Cust Tech Delivery Teams"

# Names for description sections pulled from the Test ticket
EXPECTED_RESULTS_NAME = "Expected Results"
ACTUAL_RESULTS_NAME   = "Actual Results"
IMPACT_NAME           = "Impact"  # kept for fetch compatibility; not shown in UI

# Zephyr Squad Cloud base
ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

# Feature flag: some Cloud tenants don’t expose step results via API consistently.
# Toggle to True only if your tenant supports GET/PUT of execution step results.
ENABLE_STEP_UPDATE = True

# Optional debug flag for troubleshooting API responses
DEBUG_API = False

# ============================================================
# BASIC HELPERS (AUTH / JIRA COMMON)
# ============================================================
def get_auth():
    try:
        return HTTPBasicAuth(st.secrets["JIRA_EMAIL"], st.secrets["JIRA_API_TOKEN"])
    except KeyError as e:
        st.error(f"Missing secret: {e}. Add in App → Settings → Secrets.")
        st.stop()

def headers_json():
    return {"Accept": "application/json", "Content-Type": "application/json"}

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

# -- Field ID cache & resolver --
_FIELD_ID_CACHE = {}

def get_field_id_by_name(field_name, auth):
    """
    Resolve Jira field ID by display name (case-insensitive).
    Returns the field ID or None if not found.
    """
    cached = _FIELD_ID_CACHE.get(field_name)
    if cached:
        return cached
    url = f"{JIRA_BASE_URL}/rest/api/3/field"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=60)
    r.raise_for_status()
    for f in r.json():
        name = (f.get("name") or "").strip().lower()
        if name == field_name.strip().lower():
            fid = f.get("id")
            if fid:
                _FIELD_ID_CACHE[field_name] = fid
                return fid
    return None

# ============================================================
# ZEPHYR (JWT + HELPERS) — Cloud (JWT per request; QSH from path+query)
# ============================================================
def build_zephyr_jwt(method, relative_path, query_params=None, expires_in=360):
    """
    Generate Zephyr Squad Cloud JWT with 'qsh' = SHA256(METHOD & RELATIVE_PATH & sorted(querystring)).
    - relative_path: path beginning with '/public/rest/api/1.0/...'
    - query_params: dict; must match actual request; keys sorted
    """
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
# FETCH FIELDS FROM TEST TICKET → TEMP STORE
# ============================================================
def fetch_test_ticket_fields_and_text(test_key, auth):
    """
    Pull labels/components/fixVersions/versions and custom fields by name,
    plus the Test ticket's summary, description, and text fields (Expected/Actual/Impact).
    """
    # resolve custom ids by name
    portfolio_id = get_field_id_by_name(CUST_TECH_PORTFOLIO_NAME, auth)
    product_id   = get_field_id_by_name(CUST_TECH_PRODUCT_NAME, auth)
    team_id      = get_field_id_by_name(CUST_TECH_DELIVERY_TEAM_NAME, auth)
    expected_id  = get_field_id_by_name(EXPECTED_RESULTS_NAME, auth)
    actual_id    = get_field_id_by_name(ACTUAL_RESULTS_NAME, auth)
    impact_id    = get_field_id_by_name(IMPACT_NAME, auth)

    standard   = ["summary", "labels", "components", "fixVersions", "versions", "description", "issuelinks"]
    custom_ids = [fid for fid in [portfolio_id, product_id, team_id, expected_id, actual_id, impact_id] if fid]
    fields_param = ",".join(standard + custom_ids)

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{test_key}"
    r = requests.get(
        url,
        params={"fields": fields_param},
        headers={"Accept": "application/json"},
        auth=auth,
        timeout=30
    )
    r.raise_for_status()
    f = r.json().get("fields", {})

    # Normalize/shape values
    result = {
        "summary": f.get("summary") or "",
        "labels": f.get("labels") or [],
        "components": [{"name": c.get("name")} for c in (f.get("components") or [])],
        "fixVersions": [{"name": v.get("name")} for v in (f.get("fixVersions") or [])],
        "versions": [{"name": v.get("name")} for v in (f.get("versions") or [])],
        "custom": {},
        "text": {
            "issue_description": "",  # will render in ADF
            "expected_results": "",
            "actual_results": "",
            "impact": ""
        },
        "linked_story_key": None
    }

    # description may be string or ADF doc
    desc = f.get("description")
    if isinstance(desc, dict) and desc.get("type") == "doc":
        try:
            paras = [n for n in desc.get("content", []) if n.get("type") == "paragraph"]
            result["text"]["issue_description"] = "".join(
                t.get("text", "") for t in paras[0].get("content", []) if t.get("type") == "text"
            ) if paras else ""
        except Exception:
            result["text"]["issue_description"] = ""
    else:
        result["text"]["issue_description"] = (desc or "").strip() if isinstance(desc, str) else ""

    def _copy_single_select(field_id):
        val = f.get(field_id)
        if isinstance(val, dict) and val.get("id"):
            result["custom"][field_id] = {"id": val["id"]}
        elif val:
            result["custom"][field_id] = val

    # custom single-selects (Portfolio, Product, Delivery Team)
    if portfolio_id: _copy_single_select(portfolio_id)
    if product_id:   _copy_single_select(product_id)
    if team_id:      _copy_single_select(team_id)

    # text fields — Expected/Actual/Impact
    def _get_text(field_id):
        v = f.get(field_id)
        if isinstance(v, dict) and v.get("type") == "doc":
            try:
                paras = [n for n in v.get("content", []) if n.get("type") == "paragraph"]
                return "".join(t.get("text", "") for t in paras[0].get("content", []) if t.get("type") == "text") if paras else ""
            except Exception:
                return ""
        return (v or "").strip() if isinstance(v, str) else ""

    if expected_id: result["text"]["expected_results"] = _get_text(expected_id)
    if actual_id:   result["text"]["actual_results"]   = _get_text(actual_id)
    if impact_id:   result["text"]["impact"]           = _get_text(impact_id)

    # Capture linked Story key (best-effort via issuelinks)
    for link in (f.get("issuelinks") or []):
        for side in ("outwardIssue", "inwardIssue"):
            other = link.get(side)
            if other and (other.get("fields", {}).get("issuetype", {}).get("name", "").lower() in ("story", "user story")):
                result["linked_story_key"] = other.get("key")
                break
        if result["linked_story_key"]:
            break

    return result

# ============================================================
# ADF BUILDERS (sections as requested)
# ============================================================
def adf_text(text, marks=None):
    node = {"type": "text", "text": text}
    if marks: node["marks"] = marks
    return node

def adf_paragraph(text):
    return {"type": "paragraph", "content": [adf_text(text)]}

def adf_heading(text, level=2):
    return {"type": "heading", "attrs": {"level": level}, "content": [adf_text(text)]}

def adf_bullet_list(items):
    if not items: return []
    return [{"type": "bulletList",
             "content": [{"type": "listItem","content":[adf_paragraph(i)]} for i in items]}]

def adf_ordered_list(items):
    if not items: return []
    return [{"type": "orderedList",
             "content": [{"type": "listItem","content":[adf_paragraph(i)]} for i in items]}]

# --- ADF helpers (inline strong) ---
def adf_strong_text(text: str):
    return {"type": "text", "text": text, "marks": [{"type": "strong"}]}

def adf_paragraph_segments(segments):
    """
    segments: list of tuples -> (text_str, is_bold_bool)
    """
    content = []
    for txt, bold in segments:
        if not txt:
            continue
        content.append(adf_strong_text(txt) if bold else adf_text(txt))
    return {"type": "paragraph", "content": content}

def adf_paragraph_with_bold_quotes(line: str):
    """Render a line; anything inside single quotes '...' is bold."""
    segments = []
    parts = re.split(r"(')", line)
    in_quote = False
    buf = ""
    for p in parts:
        if p == "'":
            if in_quote:
                segments.append((buf, True))
                buf = ""
                in_quote = False
            else:
                if buf:
                    segments.append((buf, False))
                    buf = ""
                in_quote = True
        else:
            buf += p
    if buf:
        segments.append((buf, False))
    return adf_paragraph_segments(segments)

# ============================================================
# AI-LITE HELPERS — Clean, business-focused style
# ============================================================
def normalize_step(s):
    s = s.strip()
    s = re.sub(r'^\s*(?:step\s*\d+[\):\-\]\s]*)', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    return s

def ai_lite_draft(context):
    """
    Produce cleaner, business‑focused content:
    - Summary: from failed step + phase
    - Issue Description: concise impact statement
    - Steps: keep, but mark failed
    - Expected: generic acceptance of selection
    - Actual: concise failure + evidence note
    """
    raw_steps = context.get("zephyr_steps") or []
    zephyr_expected = context.get("zephyr_expected") or []
    steps = [normalize_step(x) for x in raw_steps if x and x.strip()]
    failed_n = int(context.get("failed_step_num") or 0)
    idx = failed_n - 1 if failed_n >= 1 else None
    selected_step = steps[idx] if (idx is not None and idx < len(steps)) else None
    phase = (context.get("test_phase") or "").strip()

    # Extract option + field (from step text "Select 'No' from 'Field' dropdown")
    option, field = None, None
    if selected_step:
        m = re.search(r"Select\s+'([^']+)'\s+from\s+'([^']+)'", selected_step, flags=re.I)
        if m:
            option, field = m.group(1), m.group(2)

    # Expected Results
    expected_txt = (context.get("test_expected_results") or "").strip()
    if not expected_txt:
        expected_txt = "System should accept the selection and continue the workflow without errors."

    # Actual Results
    actual_txt = (context.get("test_actual_results") or "").strip()
    if not actual_txt:
        if option:
            actual_txt = f"System fails to proceed after selecting '{option}', blocking the process. See attachments for details."
        else:
            actual_txt = "System fails to proceed, blocking the process. See attachments for details."

    # Issue Description (business context first)
    if option and field:
        phase_prefix = f"During {phase} testing, " if phase else ""
        issue_desc = f"{phase_prefix}selecting '{option}' in '{field}' fails to proceed as expected, preventing completion of the workflow."
    else:
        phase_prefix = f"During {phase} testing, " if phase else ""
        issue_desc = f"{phase_prefix}the workflow fails to proceed due to a selection error."

    # Mark failed step for visibility
    if idx is not None and idx < len(steps):
        steps[idx] = f"[FAILED] {steps[idx]}"

    # Summary/title suggestion
    if option and field:
        summary = f"Incorrect behavior when selecting '{option}' in '{field}'" + (f" during {phase}" if phase else "")
    else:
        summary = f"Workflow does not proceed after selection" + (f" during {phase}" if phase else "")

    return {
        "summary_suggestion": summary,
        "issue_description": issue_desc,
        "steps_to_reproduce": steps,
        "expected_results": expected_txt,
        "actual_results": actual_txt,
        "confidence": "n/a"
    }

def make_adf_from_ai(test_key, ai, evidence_names):
    content = []

    # Issue Description
    content.append(adf_heading("Issue Description"))
    desc_txt = (ai.get("issue_description") or f"Related Test Ticket: {test_key}").strip()
    content.append(adf_paragraph(desc_txt))

    # Steps to reproduce (bold quoted phrases)
    content.append(adf_heading("Steps to reproduce"))
    steps = ai.get("steps_to_reproduce") or []
    if steps:
        content.append({"type": "orderedList", "content": [
            {"type": "listItem", "content": [adf_paragraph_with_bold_quotes(s)]} for s in steps
        ]})
    else:
        content.append(adf_paragraph("(not provided)"))

    # Expected Results
    content.append(adf_heading("Expected Results"))
    expected = (ai.get("expected_results") or "").strip()
    content.append(adf_paragraph(expected if expected else "(not provided)"))

    # Actual results
    content.append(adf_heading("Actual results"))
    actual = (ai.get("actual_results") or "").strip()
    content.append(adf_paragraph(actual if actual else "(not provided)"))

    # Evidences
    content.append(adf_heading("Evidences"))
    content.extend(adf_bullet_list(evidence_names) if evidence_names else [adf_paragraph("See attachments.")])

    return {"type": "doc", "version": 1, "content": content}

# ============================================================
# ZEPHYR: STEPS/EXPECTED, EXECUTION LOOKUP, STEP FAIL, LINK DEFECT, FAIL EXECUTION
# ============================================================
def get_zephyr_steps_and_expected(jira_test_key, auth):
    issue_id = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)
    rel = f"/public/rest/api/1.0/teststep/{issue_id}"
    rows = zephyr_get(rel, query_params={"projectId": project_id})
    steps, expected = [], []
    if isinstance(rows, list):
        for s in sorted(rows, key=lambda x: x.get("orderId", 0)):
            step_txt = (s.get("step") or "").strip()
            exp_txt  = (s.get("result") or "").strip()
            if step_txt:
                steps.append(step_txt)
            if exp_txt:
                expected.append(exp_txt)
    return steps, expected

def get_latest_execution(jira_test_key, auth):
    """
    Return dict {id, issueId, projectId, versionId, cycleId} for the latest execution.
    Cloud executions API responds with nested "execution" objects.
    """
    issue_id   = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)

    # Try direct listing
    try:
        rel = "/public/rest/api/1.0/executions"
        params = {"issueId": issue_id, "projectId": project_id, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        if DEBUG_API: st.write("DEBUG: Executions API response:", data)
        execs = (data or {}).get("executions") or []
        if execs:
            e = execs[0].get("execution") or {}
            if e.get("id"):
                return {
                    "id":        e.get("id"),
                    "issueId":   e.get("issueId"),
                    "projectId": e.get("projectId"),
                    "versionId": e.get("versionId"),
                    "cycleId":   e.get("cycleId"),
                }
    except Exception as e:
        if DEBUG_API: st.warning(f"Executions API failed: {e}")

    # Fallback: ZQL search
    try:
        rel = "/public/rest/api/1.0/zql/executeSearch"
        zql = f'issue = "{jira_test_key}" ORDER BY executionDate DESC'
        params = {"zqlQuery": zql, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        if DEBUG_API: st.write("DEBUG: ZQL API response:", data)
        execs = ((data or {}).get("searchResult") or {}).get("executions") or []
        if execs:
            e = execs[0].get("execution") or {}
            if e.get("id"):
                return {
                    "id":        e.get("id"),
                    "issueId":   e.get("issueId"),
                    "projectId": e.get("projectId"),
                    "versionId": e.get("versionId"),
                    "cycleId":   e.get("cycleId"),
                }
    except Exception as e:
        if DEBUG_API: st.warning(f"ZQL API failed: {e}")

    return None

def link_defect_to_execution(exec_obj, defect_issue_key):
    """
    Cloud: link a defect to execution via /execution/{id}/execute
    Must include issueId, projectId, versionId, cycleId.
    """
    rel = f"/public/rest/api/1.0/execution/{exec_obj['id']}/execute"
    body = {
        "issueId":   exec_obj["issueId"],
        "projectId": exec_obj["projectId"],
        "versionId": exec_obj["versionId"],
        "cycleId":   exec_obj["cycleId"],
        # Include both to satisfy older payload variants
        "defects": [defect_issue_key],
        "defectList": [defect_issue_key],
        "updateDefectList": "true"
    }
    return zephyr_put(rel, json_body=body)

def fail_zephyr_step(execution_id, failed_step_num):
    """
    Cloud: step results via /stepresult (list by executionId) and /stepresult/{id} (update).
    """
    # 1) fetch step results
    rel_list = "/public/rest/api/1.0/stepresult"
    data = zephyr_get(rel_list, query_params={"executionId": execution_id})
    if DEBUG_API: st.write("DEBUG: Step Results list:", data)

    if not isinstance(data, list) or not data:
        raise RuntimeError("No step results returned for this execution.")

    steps_sorted = sorted(data, key=lambda x: x.get("orderId", 0))
    idx = max(1, int(failed_step_num)) - 1
    if idx >= len(steps_sorted):
        idx = len(steps_sorted) - 1

    step_result_id = steps_sorted[idx].get("id")
    if not step_result_id:
        raise RuntimeError("Couldn't resolve stepResultId from Zephyr response.")

    # 2) update chosen step
    rel_update = f"/public/rest/api/1.0/stepresult/{step_result_id}"
    body = {"status": {"id": 2}}  # 2 = Fail
    zephyr_put(rel_update, json_body=body)

def fail_execution(exec_obj):
    """
    Cloud: set execution status via /execution/{id}/execute with status + IDs.
    """
    rel = f"/public/rest/api/1.0/execution/{exec_obj['id']}/execute"
    body = {
        "issueId":   exec_obj["issueId"],
        "projectId": exec_obj["projectId"],
        "versionId": exec_obj["versionId"],
        "cycleId":   exec_obj["cycleId"],
        "status": {"id": 2}  # 2 = FAIL
    }
    zephyr_put(rel, json_body=body)

# ============================================================
# EPIC LINKING (Parent/Epic)
# ============================================================
def try_set_defect_parent_to_epic(defect_key, test_fetch, auth):
    story_key = test_fetch.get("linked_story_key")
    if not story_key:
        return False, "No linked Story found on Test ticket."

    # Fetch Story to discover Epic
    url_story = f"{JIRA_BASE_URL}/rest/api/3/issue/{story_key}"
    r = requests.get(url_story, params={"fields": "parent"}, headers={"Accept":"application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    fields = r.json().get("fields", {})
    epic_key = None
    if fields.get("parent", {}).get("key"):
        epic_key = fields["parent"]["key"]
    else:
        # fallback: use 'Epic Link' custom field if available
        epic_link_fid = get_field_id_by_name("Epic Link", auth)
        if epic_link_fid:
            r2 = requests.get(url_story, params={"fields": epic_link_fid}, headers={"Accept":"application/json"}, auth=auth, timeout=30)
            r2.raise_for_status()
            el = r2.json().get("fields", {}).get(epic_link_fid)
            if isinstance(el, dict) and el.get("key"):
                epic_key = el.get("key")
            elif isinstance(el, str) and el:
                epic_key = el

    if not epic_key:
        return False, "No Epic found for linked Story."

    # First attempt: set 'parent' to Epic
    try:
        r3 = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{defect_key}",
            json={"fields": {"parent": {"key": epic_key}}},
            headers=headers_json(),
            auth=auth,
            timeout=30
        )
        if r3.status_code in (200, 204):
            return True, f"Parent set to Epic {epic_key}."
    except Exception:
        pass

    # Fallback: try 'Epic Link' field edit
    epic_link_fid = get_field_id_by_name("Epic Link", auth)
    if epic_link_fid:
        r4 = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{defect_key}",
            json={"fields": {epic_link_fid: epic_key}},
            headers=headers_json(),
            auth=auth,
            timeout=30
        )
        if r4.status_code in (200, 204):
            return True, f"Epic Link set to {epic_key}."
        return False, f"Failed to set Epic Link: {r4.status_code} {r4.text[:300]}"
    return False, "Epic Link field not available on this project."

# ============================================================
# STREAMLIT UI
# ============================================================
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 AutoDefect Logger")
st.markdown("**Fields marked with * are mandatory**")

test_ticket     = st.text_input("Test Ticket Number * (e.g. CT-12345)", value="")
failed_step_num = st.number_input("Failed Test Step Number *", min_value=1, value=3, step=1)
severity        = st.selectbox("Severity *", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
priority        = st.selectbox("Priority *", ["Critical", "Major", "Medium", "Minor"])
test_phase      = st.selectbox("Test Phase *", ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"])
uploaded_files  = st.file_uploader("📎 Attach Evidence (screenshots, logs)", accept_multiple_files=True)

# ============================================================
# AI-LITE GENERATION
# ============================================================
use_ai = st.checkbox("Use AI-lite to draft description & steps (beta)", value=True)

if st.button("🧠 Generate Draft (AI-lite)"):
    if not test_ticket.strip():
        st.error("Enter the Test Ticket key first.")
        st.stop()
    auth = get_auth()

    # Background pulls
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip(), auth)
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")

    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip(), auth)
    except Exception as e:
        copied = {
            "summary": "",
            "labels": [], "components": [], "fixVersions": [], "versions": [],
            "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""},
            "linked_story_key": None
        }
        st.warning(f"Could not fetch fields/description from Test: {e}")

    evidence_names = [f.name for f in (uploaded_files or [])]

    ctx = {
        "project_key": PROJECT_KEY,
        "test_key": test_ticket.strip(),
        "failed_step_num": int(failed_step_num),
        "severity": severity,
        "priority": priority,
        "test_phase": test_phase,
        "zephyr_steps": steps_list,
        "zephyr_expected": expected_from_zephyr,
        "test_issue_description": copied["text"]["issue_description"],
        "test_expected_results": copied["text"]["expected_results"],
        "test_actual_results": copied["text"]["actual_results"],
        "labels": copied.get("labels", []),
        "components": copied.get("components", []),
        "versions": copied.get("versions", []),
        "fixVersions": copied.get("fixVersions", []),
        "evidence_names": evidence_names
    }
    ai_out = ai_lite_draft(ctx)
    st.session_state["ai_out"] = ai_out
    st.session_state["ctx"]    = ctx
    st.success("AI-lite draft generated. Review and edit below.")

# Editable preview (Impact & AI notes removed)
if use_ai and "ai_out" in st.session_state:
    ai = st.session_state["ai_out"]
    ai["issue_description"] = st.text_area("Issue Description", ai.get("issue_description", ""), height=120)
    steps_txt = "\n".join(ai.get("steps_to_reproduce", []))
    steps_txt = st.text_area("Steps to reproduce (one per line)", steps_txt, height=150)
    ai["steps_to_reproduce"] = [s.strip() for s in steps_txt.splitlines() if s.strip()]
    ai["expected_results"] = st.text_area("Expected Results", ai.get("expected_results", ""), height=100)
    ai["actual_results"]   = st.text_area("Actual Results",   ai.get("actual_results",   ""), height=100)

# ============================================================
# HELPERS: SUMMARY, TRANSITION TO FAILED
# ============================================================
def build_defect_summary_from_test(copied):
    base = (copied.get("summary") or "").strip()
    if base and base.lower() not in ("testing", "test", "defect"):
        return base
    ai = st.session_state.get("ai_out")
    if ai and ai.get("summary_suggestion"):
        return ai["summary_suggestion"]
    return "Observed issue during test execution"

def transition_issue_to_failed(issue_key, auth):
    # Discover transition id named 'Failed' (or 'Fail') dynamically
    r = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/transitions",
        headers={"Accept":"application/json"},
        auth=auth,
        timeout=30
    )
    if r.status_code != 200:
        st.warning(f"Transitions fetch failed: {r.status_code} {r.text[:300]}")
        return False
    transitions = (r.json() or {}).get("transitions", [])
    target = None
    for t in transitions:
        name   = (t.get("name") or "").strip().lower()
        to_name = (t.get("to", {}).get("name") or "").strip().lower()
        if name in ("failed", "fail") or to_name in ("failed", "fail", "fail status", "fail state", "fail"):
            target = t.get("id")
            break
    if not target:
        for t in transitions:
            if str(t.get("id")) == "51":
                target = "51"
                break
    if not target:
        st.warning("Could not locate a 'Failed' transition for this issue.")
        return False

    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/transitions",
        json={"transition": {"id": str(target)}},
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if resp.status_code not in (200, 204):
        st.warning(f"Failed to update Test ticket status: {resp.status_code} {resp.text[:300]}")
        return False
    return True

# ============================================================
# CREATE DEFECT
# ============================================================
if st.button("🚀 Create Defect"):
    if not all([test_ticket.strip(), severity, priority, test_phase]):
        st.error("Please fill all mandatory fields (*)")
        st.stop()

    auth = get_auth()

    # Resolve issue type & field meta
    url_it = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes"
    r_it = requests.get(url_it, headers={"Accept":"application/json"}, auth=auth, timeout=30)
    if r_it.status_code != 200:
        st.error(f"Failed to fetch issue types: {r_it.status_code} {r_it.text[:300]}")
        st.stop()
    it_by_name = {it.get("name"): it.get("id") for it in (r_it.json().get("issueTypes") or []) if it.get("id")}
    issue_type_id = next((it_by_name[n] for n in ISSUE_TYPE_CANDIDATES if n in it_by_name), None) or (r_it.json().get("issueTypes") or [{}])[0].get("id")

    fields_meta = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes/{issue_type_id}",
        headers={"Accept":"application/json"},
        auth=auth,
        timeout=30
    ).json()

    def get_option_id(fields_meta_obj, field_id, chosen_label):
        for f in fields_meta_obj.get("fields", []):
            if f.get("fieldId") == field_id:
                for opt in (f.get("allowedValues") or []):
                    label = opt.get("value") or opt.get("name")
                    if label == chosen_label:
                        return opt.get("id")
        return None

    severity_id   = get_option_id(fields_meta, SEVERITY_FIELD_ID, severity)
    test_phase_id = get_option_id(fields_meta, TEST_PHASE_FIELD_ID, test_phase)
    if not severity_id or not test_phase_id:
        st.warning("Severity or Test Phase option not found on create screen; will try label fallback.")

    # Background pulls
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip(), auth)
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")

    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip(), auth)
    except Exception as e:
        copied = {
            "summary": "",
            "labels": [], "components": [], "fixVersions": [], "versions": [],
            "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""},
            "linked_story_key": None
        }
        st.warning(f"Could not fetch fields/description from Test: {e}")

    evidence_names = [f.name for f in (uploaded_files or [])]

    # Ensure AI‑lite context exists for clean style
    if use_ai and "ai_out" not in st.session_state:
        ctx = {
            "project_key": PROJECT_KEY,
            "test_key": test_ticket.strip(),
            "failed_step_num": int(failed_step_num),
            "severity": severity,
            "priority": priority,
            "test_phase": test_phase,
            "zephyr_steps": steps_list,
            "zephyr_expected": expected_from_zephyr,
            "test_issue_description": copied["text"]["issue_description"],
            "test_expected_results": copied["text"]["expected_results"],
            "test_actual_results": copied["text"]["actual_results"],
            "labels": copied.get("labels", []),
            "components": copied.get("components", []),
            "versions": copied.get("versions", []),
            "fixVersions": copied.get("fixVersions", []),
            "evidence_names": evidence_names
        }
        st.session_state["ai_out"] = ai_lite_draft(ctx)

    # Summary/title improved (uses Test summary if meaningful, else AI suggestion)
    summary = build_defect_summary_from_test(copied)

    # Build description (ADF — Jira Cloud requires ADF)
    if use_ai and "ai_out" in st.session_state:
        description_adf = make_adf_from_ai(
            test_key=test_ticket.strip(),
            ai=st.session_state["ai_out"],
            evidence_names=evidence_names
        )
    else:
        description_adf = {
            "type": "doc", "version": 1,
            "content": [adf_heading("Issue Description"),
                        adf_paragraph(copied["text"]["issue_description"] or f"Related Test Ticket: {test_ticket.strip()}")]
        }

    # Create payload — ensure description is ADF; use option ids if available, else label values
    create_fields = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"id": issue_type_id},
        "summary": summary,
        "description": description_adf,
        "priority": {"name": priority}
    }
    if severity_id:
        create_fields[SEVERITY_FIELD_ID] = {"id": severity_id}
    else:
        create_fields[SEVERITY_FIELD_ID] = {"value": severity}
    if test_phase_id:
        create_fields[TEST_PHASE_FIELD_ID] = {"id": test_phase_id}
    else:
        create_fields[TEST_PHASE_FIELD_ID] = {"value": test_phase}

    create_payload = {"fields": create_fields}

    # Create issue
    create_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=create_payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if create_resp.status_code != 201:
        st.error("❌ Defect creation failed")
        st.code(create_resp.text)
        st.stop()

    issue_key = create_resp.json()["key"]
    st.session_state["issue_key"] = issue_key  # Persist for reruns
    st.success(f"✅ Defect created: {issue_key}")

    # -- Update copied fields on the new defect --
    edit_fields = {}
    if copied["labels"]:
        edit_fields["labels"] = [lbl for lbl in copied["labels"] if lbl != "JiraTestGenAI"]
    if copied["components"]:  edit_fields["components"]  = copied["components"]
    if copied["fixVersions"]: edit_fields["fixVersions"] = copied["fixVersions"]
    if copied["versions"]:    edit_fields["versions"]    = copied["versions"]
    for fid, val in copied["custom"].items():
        edit_fields[fid] = val

    if edit_fields:
        edit_resp = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}",
            json={"fields": edit_fields},
            headers=headers_json(),
            auth=auth,
            timeout=30
        )
        if edit_resp.status_code not in (200, 204):
            st.warning(f"Field copy update returned {edit_resp.status_code}: {edit_resp.text[:300]}")
        else:
            st.success("🔁 Copied Labels/Components/Versions/Custom fields from the Test ticket.")

    # Parent/Epic linking (best‑effort)
    ok, msg = try_set_defect_parent_to_epic(issue_key, copied, auth)
    st.info(f"Epic link: {msg}")

    # Link defect ↔ test ticket
    link_payload = {"type": {"name": "Relates"}, "inwardIssue": {"key": test_ticket.strip()}, "outwardIssue": {"key": issue_key}}
    link_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issueLink",
        json=link_payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if link_resp.status_code not in (200, 201, 204):
        st.warning(f"Linking returned {link_resp.status_code}: {link_resp.text[:300]}")

    # ---- Upload attachments (if the user added any)
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
                st.warning(f"Attachment '{file.name}' failed: {attach_resp.status_code} {attach_resp.text[:200]}")
        st.success("📎 Attachments uploaded, fields synced & Test Ticket linked")

    # Transition Test ticket to Failed (dynamic lookup; falls back to id '51')
    transitioned = transition_issue_to_failed(test_ticket.strip(), auth)
    if transitioned:
        st.success("✅ Test ticket transitioned to 'Failed'.")

    # Zephyr: link defect to latest execution, fail the failed step & execution
    try:
        exec_obj = get_latest_execution(test_ticket.strip(), auth)
        if not exec_obj:
            st.warning("No Zephyr execution found for this Test.")
        else:
            # Link defect (Cloud via /execute with IDs)
            try:
                link_defect_to_execution(exec_obj, issue_key)
                st.success("🔗 Defect linked to Zephyr execution (Defects section).")
            except Exception as e:
                st.warning(f"Failed to link defect to Zephyr execution: {e}")

            # Update failed step (best-effort; may be unsupported on some tenants)
            try:
                if ENABLE_STEP_UPDATE:
                    fail_zephyr_step(exec_obj["id"], failed_step_num)
                    st.success("❗ Failed step updated in Zephyr.")
                else:
                    st.info("Step update skipped (ENABLE_STEP_UPDATE = False).")
            except Exception as e:
                st.warning(f"Could not update Zephyr failed step: {e}")

            # Fail the overall execution (Cloud via /execute with IDs)
            try:
                fail_execution(exec_obj)
                st.success("🟥 Zephyr execution status set to FAIL.")
            except Exception as e:
                st.warning(f"Failed to update Zephyr execution status: {e}")
    except Exception as e:
        st.warning(f"Zephyr operations failed: {e}")

    # Safe link rendering (works across reruns)
    ik = st.session_state.get("issue_key")
    if ik:
        st.link_button("Open Defect in Jira", f"{JIRA_BASE_URL}/browse/{ik}")
    else:
        st.info("Create a defect first to enable the Jira link.")
# - Improved defect title (uses Test summary or AI suggestion)
# - Removed Impact & AI Notes from UI and ADF description
# - Clearer description sections with bold highlights in steps
# - After linking, transition Test ticket to "Failed" (dynamic transition lookup)
# - ADF description enforced; safe link rendering; attachments upload
# - Zephyr Cloud fixes:
