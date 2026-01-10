
# app.py
# --------------------------------------------------------------
# Jira Defect Creator (background Zephyr fetch + field copy from Test)
# --------------------------------------------------------------
# Prereqs:
#  - Streamlit secrets: JIRA_EMAIL, JIRA_API_TOKEN,
#    ZEPHYR_ACCESS_KEY, ZEPHYR_SECRET_KEY, ATLASSIAN_ACCOUNT_ID
#  - pip install streamlit requests
# --------------------------------------------------------------

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

import streamlit as st
import requests
from requests.auth import HTTPBasicAuth

# ============================================================
# CONFIGURATION
# ============================================================
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"

# Prefer these issue types when creating the defect
ISSUE_TYPE_CANDIDATES = ["Defect", "Bug"]

# Custom field IDs in your Jira (single-selects already known)
TEST_PHASE_FIELD_ID = "customfield_10245"   # Test Phase
SEVERITY_FIELD_ID   = "customfield_10260"   # Severity

# Names of additional fields you want to copy by NAME -> ID resolution at runtime
CUST_TECH_PORTFOLIO_NAME     = "Cust Tech Portfolio"
CUST_TECH_PRODUCT_NAME       = "Cust Tech Product"
CUST_TECH_DELIVERY_TEAM_NAME = "Cust Tech Delivery Team"

# Zephyr Cloud base
ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

# ============================================================
# BASIC HELPERS (AUTH / JIRA COMMON)
# ============================================================

def get_auth():
    """Create Jira HTTP Basic auth from secrets."""
    try:
        return HTTPBasicAuth(st.secrets["JIRA_EMAIL"], st.secrets["JIRA_API_TOKEN"])
    except KeyError as e:
        st.error(f"Missing secret: {e}. Add it in App settings → Secrets.")
        st.stop()

def headers_json():
    return {"Accept": "application/json", "Content-Type": "application/json"}

def jira_issue_id_from_key(issue_key: str) -> str:
    """Jira Cloud v3: GET /rest/api/3/issue/{key} → internal issueId"""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth(), timeout=30)
    r.raise_for_status()
    return r.json()["id"]

def jira_project_id_from_key(project_key: str) -> int:
    """Jira Cloud v3: GET /rest/api/3/project/{projectKey} → numeric project id"""
    url = f"{JIRA_BASE_URL}/rest/api/3/project/{project_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth(), timeout=30)
    r.raise_for_status()
    return int(r.json()["id"])

# ------------------------------------------------------------
# Resolve FIELD ID by NAME (for your "Cust Tech ..." custom fields)
# ------------------------------------------------------------
_FIELD_ID_CACHE: dict[str, str] = {}

def get_field_id_by_name(field_name: str) -> str | None:
    """Look up field ID by its display name (case-insensitive)."""
    if field_name in _FIELD_ID_CACHE:
        return _FIELD_ID_CACHE[field_name]
    url = f"{JIRA_BASE_URL}/rest/api/3/field"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth(), timeout=60)
    r.raise_for_status()
    for f in r.json():
        if (f.get("name") or "").strip().lower() == field_name.strip().lower():
            _FIELD_ID_CACHE[field_name] = f["id"]
            return f["id"]
    return None

# ============================================================
# ZEPHYR (JWT + GET TEST STEPS)
# ============================================================

def build_zephyr_jwt(method: str, relative_path: str, query_params=None, expires_in: int = 360) -> str:
    """
    Build per-request JWT for Zephyr Squad Cloud.
    JWT payload includes qsh (METHOD & PATH & canonical_query).
    Headers required in request:
      Authorization: JWT <token>
      zapiAccessKey: <your access key>
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

    def b64(obj: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")

    signing_input = b".".join([b64(header), b64(payload)])
    signature = base64.urlsafe_b64encode(
        hmac.new(st.secrets["ZEPHYR_SECRET_KEY"].encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return signing_input.decode() + "." + signature.decode()

def zephyr_get(relative_path: str, query_params=None):
    """GET wrapper for Zephyr Cloud with JWT + zapiAccessKey headers."""
    query_params = query_params or {}
    jwt = build_zephyr_jwt("GET", relative_path, query_params)
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": st.secrets["ZEPHYR_ACCESS_KEY"],
        "Accept": "application/json",
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.get(url, headers=headers, params=query_params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr GET {relative_path} failed {r.status_code}: {r.text[:300]}")
    return r.json()

def get_zephyr_steps_only(jira_test_key: str) -> list[str]:
    """
    Fetch Zephyr design-time steps and return **only the step descriptions**.
    (You requested Expected / Actual to be separate free-text fields on the defect.)
    """
    issue_id = jira_issue_id_from_key(jira_test_key)
    project_id = jira_project_id_from_key(PROJECT_KEY)  # "CT" → numeric id
    rel = f"/public/rest/api/1.0/teststep/{issue_id}"
    rows = zephyr_get(rel, query_params={"projectId": project_id})
    steps = []
    for s in sorted(rows, key=lambda x: x.get("orderId", 0)):
        txt = (s.get("step") or "").strip()
        if txt:
            steps.append(txt)
    return steps

# ============================================================
# FETCH FIELDS FROM TEST TICKET → TEMP STORE (labels/components/versions/customs)
# ============================================================

def fetch_test_ticket_fields(test_key: str) -> dict:
    """
    Pull selected fields from the source Test ticket and return a normalized dict:
        {
          "labels": [...],
          "components": [{"name": ...}, ...],
          "fixVersions": [{"name": ...}, ...],
          "versions": [{"name": ...}, ...],  # Affects Version/s
          "custom": { "<customfield_id>": {"id": "<optionId>"} | value }
        }
    """
    # resolve custom field ids by name
    portfolio_id = get_field_id_by_name(CUST_TECH_PORTFOLIO_NAME)
    product_id   = get_field_id_by_name(CUST_TECH_PRODUCT_NAME)
    team_id      = get_field_id_by_name(CUST_TECH_DELIVERY_TEAM_NAME)

    wanted = ["labels", "components", "fixVersions", "versions"]
    custom_ids = [fid for fid in [portfolio_id, product_id, team_id] if fid]
    fields_param = ",".join(wanted + custom_ids)

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{test_key}"
    r = requests.get(
        url,
        params={"fields": fields_param},
        headers={"Accept": "application/json"},
        auth=get_auth(),
        timeout=30
    )
    r.raise_for_status()
    f = r.json().get("fields", {})

    result = {
        "labels": f.get("labels") or [],
        "components": [{"name": c.get("name")} for c in (f.get("components") or [])],
        "fixVersions": [{"name": v.get("name")} for v in (f.get("fixVersions") or [])],
        "versions": [{"name": v.get("name")} for v in (f.get("versions") or [])],
        "custom": {}
    }

    # helper to store single-select custom option as {"id": "<optionId>"}
    def _set_single_select(field_id):
        val = f.get(field_id)
        if isinstance(val, dict) and val.get("id"):
            result["custom"][field_id] = {"id": val["id"]}
        elif val:  # sometimes apps return plain strings or dicts w/ name
            result["custom"][field_id] = val

    if portfolio_id:
        _set_single_select(portfolio_id)
    if product_id:
        _set_single_select(product_id)
    if team_id:
        _set_single_select(team_id)

    return result

# ============================================================
# ADF BUILDERS (sections as requested)
# ============================================================

def adf_text(text: str, marks: list | None = None):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node

def adf_paragraph(text: str):
    return {"type": "paragraph", "content": [adf_text(text)]}

def adf_heading(text: str, level: int = 2):
    return {"type": "heading", "attrs": {"level": level}, "content": [adf_text(text)]}

def adf_bullet_list(items: list[str]):
    if not items:
        return []
    return [{
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [adf_paragraph(i)]} for i in items]
    }]

def adf_ordered_list(items: list[str]):
    if not items:
        return []
    return [{
        "type": "orderedList",
        "content": [{"type": "listItem", "content": [adf_paragraph(i)]} for i in items]
    }]

def make_adf_description(
    test_key: str,
    issue_description: str,
    steps_list: list[str],
    expected_results: str,
    actual_results: str,
    impact: str,
    evidence_names: list[str]
) -> dict:
    """
    Build ADF with sections:
    - Issue Description
    - Steps to reproduce (ordered list)
    - Expected Results
    - Actual results
    - Impact
    - Evidences (bullet list of file names)
    """
    content = []

    # 1) Issue Description
    content.append(adf_heading("Issue Description", level=2))
    intro = issue_description.strip() or f"Related Test Ticket: {test_key}"
    content.append(adf_paragraph(intro))

    # 2) Steps to reproduce
    content.append(adf_heading("Steps to reproduce", level=2))
    if steps_list:
        content.extend(adf_ordered_list(steps_list))
    else:
        content.append(adf_paragraph("Steps not available from Zephyr."))

    # 3) Expected Results
    content.append(adf_heading("Expected Results", level=2))
    content.append(adf_paragraph(expected_results.strip() or "(not provided)"))

    # 4) Actual results
    content.append(adf_heading("Actual results", level=2))
    content.append(adf_paragraph(actual_results.strip() or "(not provided)"))

    # 5) Impact
    content.append(adf_heading("Impact", level=2))
    content.append(adf_paragraph(impact.strip() or "(not provided)"))

    # 6) Evidences
    content.append(adf_heading("Evidences", level=2))
    if evidence_names:
        content.extend(adf_bullet_list(evidence_names))
    else:
        content.append(adf_paragraph("See attachments."))

    return {"type": "doc", "version": 1, "content": content}

# ============================================================
# STREAMLIT UI (no Zephyr button; fields per your sections)
# ============================================================

st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Create Jira Defect from Test Ticket")
st.markdown("**Fields marked with * are mandatory**")

test_ticket       = st.text_input("Test Ticket Number * (e.g. CT-12345)", value="")
failed_step_num   = st.number_input("Failed Test Step Number *", min_value=1, value=1, step=1)

severity          = st.selectbox("Severity *", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
priority          = st.selectbox("Priority *", ["Critical", "Major", "Medium", "Minor"])
test_phase        = st.selectbox("Test Phase *", ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"])

issue_description = st.text_area("Issue Description *", placeholder="Summarize the problem context, where it occurs, preconditions…")
expected_results  = st.text_area("Expected Results *", placeholder="What should happen?")
actual_results    = st.text_area("Actual results *", placeholder="What actually happened?")
impact            = st.text_area("Impact *", placeholder="Business/user impact, risk, downstream effects…")

uploaded_files    = st.file_uploader("Attach Evidence (screenshots, logs)", accept_multiple_files=True)

# ============================================================
# CREATE DEFECT
# ============================================================
if st.button("🚀 Create Defect"):
    # Basic validation
    if not all([
        test_ticket.strip(),
        severity, priority, test_phase,
        issue_description.strip(),
        expected_results.strip(),
        actual_results.strip(),
        impact.strip()
    ]):
        st.error("Please fill all mandatory fields (*)")
        st.stop()

    # Resolve issue type & field meta
    issue_type_id, _issue_type_name = get_issue_type_id(PROJECT_KEY, ISSUE_TYPE_CANDIDATES)
    fields_meta = get_create_fields(PROJECT_KEY, issue_type_id)

    # Map single-selects to IDs
    severity_id   = get_option_id(fields_meta, SEVERITY_FIELD_ID, severity)
    test_phase_id = get_option_id(fields_meta, TEST_PHASE_FIELD_ID, test_phase)
    missing = []
    if severity_id is None:
        missing.append(f"Severity '{severity}' (no matching option ID)")
    if test_phase_id is None:
        missing.append(f"Test Phase '{test_phase}' (no matching option ID)")
    if missing:
        st.error("Configuration mismatch:\n- " + "\n".join(missing))
        st.stop()

    # --- Background pulls ---
    # a) Zephyr steps (ordered list of step descriptions)
    try:
        steps_list = get_zephyr_steps_only(test_ticket.strip())
    except Exception as e:
        steps_list = []
        st.warning(f"Zephyr steps could not be fetched: {e}")

    # b) Field copy from the source Test ticket (labels/components/versions/customs)
    try:
        copied_fields = fetch_test_ticket_fields(test_ticket.strip())
    except Exception as e:
        copied_fields = {"labels": [], "components": [], "fixVersions": [], "versions": [], "custom": {}}
        st.warning(f"Could not fetch fields from Test ticket: {e}")

    # Evidence file names for description (attachments sent after creation)
    evidence_names = [f.name for f in (uploaded_files or [])]

    # Summary & Description (ADF with requested sections)
    summary = f"[{test_ticket.strip()}] Failed at Step {int(failed_step_num)}"
    description_adf = make_adf_description(
        test_key=test_ticket.strip(),
        issue_description=issue_description,
        steps_list=steps_list,
        expected_results=expected_results,
        actual_results=actual_results,
        impact=impact,
        evidence_names=evidence_names
    )

    # Jira credentials
    auth = get_auth()

    # Build minimal payload for CREATE
    payload = {
        "fields": {
            "project":   {"key": PROJECT_KEY},
            "issuetype": {"id": issue_type_id},
            "summary":   summary,
            "description": description_adf,
            "priority":  {"name": priority},
            SEVERITY_FIELD_ID:   {"id": severity_id},
            TEST_PHASE_FIELD_ID: {"id": test_phase_id},
        }
    }

    # Create issue
    create_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if create_resp.status_code != 201:
        st.error("❌ Defect creation failed")
        st.code(create_resp.text)
        st.stop()

    issue_key = create_resp.json()["key"]
    st.success(f"✅ Defect created successfully: {issue_key}")

    # --------------------------------------------------------
    # SECOND STEP: Update copied fields on the new Defect
    # --------------------------------------------------------
    edit_fields = {}
    # system fields (replace with source values)
    if copied_fields["labels"]:
        edit_fields["labels"] = copied_fields["labels"]
    if copied_fields["components"]:
        edit_fields["components"] = copied_fields["components"]          # [{"name": "..."}]
    if copied_fields["fixVersions"]:
        edit_fields["fixVersions"] = copied_fields["fixVersions"]        # [{"name": "..."}]
    if copied_fields["versions"]:
        edit_fields["versions"] = copied_fields["versions"]              # [{"name": "..."}] (Affects Version/s)

    # custom fields
    for fid, val in copied_fields["custom"].items():
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

    # Link defect ↔ test ticket (Relates is symmetric)
    link_payload = {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": test_ticket.strip()},
        "outwardIssue": {"key": issue_key}
    }
    link_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issueLink",
        json=link_payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if link_resp.status_code not in (200, 201, 204):
        st.warning(f"Linking returned {link_resp.status_code}: {link_resp.text[:300]}")

    # Attach files
    if uploaded_files:
        for file in uploaded_files:
            attach_headers = {
                "X-Atlassian-Token": "no-check",
                "Accept": "application/json"
            }
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
    st.link_button("Open in Jira", f"{JIRA_BASE_URL}/browse/{issue_key}")
