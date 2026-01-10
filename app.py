
import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
import base64, hashlib, hmac, json, time, urllib.parse

# ---------------------------
# CONFIGURATION
# ---------------------------
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"

# We will try these in order and resolve to an ID for your project
ISSUE_TYPE_CANDIDATES = ["Defect", "Bug"]

# Custom field IDs in your Jira
severity = st.selectbox("Severity *", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])TEST_PHASE_FIELD_ID = "customfield_10245"   # Test Phase (single-select)
priority = st.selectbox("Priority *", ["Critical", "Major", "Medium", "Minor"])

test_phase = st.selectbox(
    "Test Phase *",
    ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"]
)

uploaded_files = st.file_uploader(
    "Attach Evidence (screenshots, logs)",
    accept_multiple_files=True
)

# --- Zephyr UI controls (added) ---
st.markdown("### Zephyr: Fetch Test Details (optional)")
if st.button("🔎 Fetch Zephyr Test Details"):
    try:
        # store markdown preview in session for later use
        zephyr_steps = get_zephyr_test_details_for_key(test_ticket)
        md = steps_to_markdown(zephyr_steps)
        st.session_state["zephyr_steps_md"] = md
        st.session_state["zephyr_steps_raw"] = zephyr_steps
        st.success("Fetched Zephyr test steps.")
        st.code(md, language="markdown")
    except Exception as e:
        st.error(f"Failed to fetch Zephyr test steps: {e}")

# ---------------------------
# HELPERS (Jira REST)
# ---------------------------

def get_auth():
    try:
        return HTTPBasicAuth(st.secrets["JIRA_EMAIL"], st.secrets["JIRA_API_TOKEN"])
    except KeyError as e:
        st.error(f"Missing secret: {e}. Add it in App settings → Secrets.")
        st.stop()

def headers_json():
    return {"Accept": "application/json", "Content-Type": "application/json"}

def get_issue_type_id(project_key: str, candidates: list[str]) -> tuple[str, str] | None:
    """
    Resolve a valid issue type ID for this project, preferring names in 'candidates'.
    Jira Cloud v3: GET /rest/api/3/issue/createmeta/{projectKey}/issuetypes
    Returns (issueTypeId, issueTypeName) or None.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{project_key}/issuetypes"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth())
    if r.status_code != 200:
        st.error(f"Failed to fetch issue types for project {project_key} ({r.status_code}): {r.text[:300]}")
        st.stop()

    issue_types = (r.json().get("issueTypes") or r.json().get("issuetypes") or [])
    if not issue_types:
        st.error(f"No issue types available for project {project_key}.")
        st.stop()

    # prefer candidates
    by_name = {it.get("name"): it.get("id") for it in issue_types if it.get("id") and it.get("name")}
    for name in candidates:
        if name in by_name:
            return by_name[name], name

    # fallback to first
    first = issue_types[0]
    return first.get("id"), first.get("name")

def get_create_fields(project_key: str, issue_type_id: str) -> dict:
    """
    Jira Cloud v3: GET /rest/api/3/issue/createmeta/{projectKey}/issuetypes/{issueTypeId}
    Returns the field metadata for create.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth())
    if r.status_code != 200:
        st.error(f"Failed to get create-field metadata ({r.status_code}): {r.text[:300]}")
        st.stop()
    return r.json()

def get_option_id(fields_meta: dict, field_id: str, chosen_label: str) -> str | None:
    """
    From create fields metadata, find the single-select option ID for the chosen label.
    """
    for f in fields_meta.get("fields", []):
        if f.get("fieldId") == field_id:
            for opt in (f.get("allowedValues") or []):
                label = opt.get("value") or opt.get("name")
                if label == chosen_label:
                    return opt.get("id")
    return None

# ---------------------------
# ZEHPYR (added)
# ---------------------------

ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

def jira_issue_id_from_key(issue_key: str) -> str:
    """
    Translate Jira issue key -> internal numeric issueId (needed by Zephyr teststep).
    Jira Cloud v3: GET /rest/api/3/issue/{key}
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth())
    r.raise_for_status()
    return r.json()["id"]

def build_zephyr_jwt(method: str, relative_path: str, query_params: dict | None = None, expires_in: int = 360) -> str:
    """
    Build per-request JWT for Zephyr Squad Cloud.
    Payload includes qsh (query string hash) calculated from METHOD & PATH & canonical_query.
    Docs: Zephyr Cloud REST uses /connect, JWT + zapiAccessKey headers per request.
    """
    method = method.upper()
    query_params = query_params or {}
    canonical_qs = urllib.parse.urlencode(sorted(query_params.items()), doseq=True)
    canonical_req = f"{method}&{relative_path}&{canonical_qs}"
    qsh = hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()

    now = int(time.time())
    payload = {
        "sub": st.secrets["ATLASSIAN_ACCOUNT_ID"],   # your Atlassian account ID
        "qsh": qsh,
        "iss": st.secrets["ZEPHYR_ACCESS_KEY"],      # access key
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

def zephyr_get(relative_path: str, query_params: dict | None = None):
    """
    GET wrapper for Zephyr Cloud with JWT + zapiAccessKey.
    """
    query_params = query_params or {}
    jwt = build_zephyr_jwt("GET", relative_path, query_params)
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": st.secrets["ZEPHYR_ACCESS_KEY"],
        "Accept": "application/json"
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.get(url, headers=headers, params=query_params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr GET {relative_path} failed {r.status_code}: {r.text[:300]}")
    return r.json()

def get_zephyr_test_details_for_key(jira_test_key: str) -> list[dict]:
    """
    Fetch design-time test steps (Step/Data/Expected) for a Jira Test issue key.
    Cloud endpoint: /public/rest/api/1.0/teststep/{issueId}
    """
    issue_id = jira_issue_id_from_key(jira_test_key)
    rel = f"/public/rest/api/1.0/teststep/{issue_id}"
    return zephyr_get(rel)

def steps_to_markdown(steps: list[dict]) -> str:
    """
    Render Zephyr steps to Markdown for preview.
    Each item typically includes: orderId, step, data, result (expected).
    """
    if not steps:
        return "*No steps found*"
    lines = []
    for s in sorted(steps, key=lambda x: x.get("orderId", 0)):
        step = (s.get("step") or "").strip()
        data = (s.get("data") or "").strip()
        exp  = (s.get("result") or "").strip()
        lines.append(f"- **Step**: {step}\n  - **Data**: {data}\n  - **Expected**: {exp}")
    return "\n".join(lines)

# ---------------------------
# DESCRIPTION (ADF)
# ---------------------------

def make_adf_description(
    test_key: str,
    step_num: int,
    severity_label: str,
    priority_label: str,
    test_phase_label: str
) -> dict:
    """
    Atlassian Document Format (ADF) description, augmented with Zephyr steps (if fetched).
    """
    # base text block
    lines = [
        f"Test Ticket: {test_key}",
        f"Failed Step Number: {step_num}",
        f"Severity: {severity_label}",
        f"Priority: {priority_label}",
        f"Test Phase: {test_phase_label}",
    ]

    content_blocks = [
        {"type": "paragraph", "content": [{"type": "text", "text": "\n".join(lines)}]}
    ]

    # append Zephyr steps if present
    z_md = st.session_state.get("zephyr_steps_md", "").strip()
    if z_md:
        content_blocks.append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Zephyr Steps"}]})
        # ADF doesn't understand Markdown; include as plain text paragraphs
        for para in z_md.split("\n"):
            content_blocks.append({"type": "paragraph", "content": [{"type": "text", "text": para}]})

    return {"type": "doc", "version": 1, "content": content_blocks}

# ---------------------------
# CREATE DEFECT BUTTON
# ---------------------------
if st.button("🚀 Create Defect"):
    # Basic validation
    if not test_ticket.strip() or not severity or not priority or not test_phase:
        st.error("Please fill all mandatory fields (*)")
        st.stop()

    # Resolve a valid issue type ID for the project
    issue_type_id, issue_type_name = get_issue_type_id(PROJECT_KEY, ISSUE_TYPE_CANDIDATES)

    # Fetch create-field metadata (to map single-select options to IDs)
    fields_meta = get_create_fields(PROJECT_KEY, issue_type_id)

    # Map Severity/Test Phase to option IDs (Jira requires IDs for single-selects)
    severity_id = get_option_id(fields_meta, SEVERITY_FIELD_ID, severity)
    test_phase_id = get_option_id(fields_meta, TEST_PHASE_FIELD_ID, test_phase)

    missing = []
    if severity_id is None:
        missing.append(f"Severity '{severity}' (no matching option ID)")
    if test_phase_id is None:
        missing.append(f"Test Phase '{test_phase}' (no matching option ID)")
    if missing:
        st.error("Configuration mismatch:\n- " + "\n- ".join(missing) + "\n\n"
                 "Ask your Jira admin to ensure these options exist in the create screen for this issue type.")
        st.stop()

    # Build summary & description (ADF now includes Zephyr steps if fetched)
    summary = f"[{test_ticket.strip()}] Failed at Step {int(failed_step_num)}"
    description_adf = make_adf_description(
        test_ticket.strip(), int(failed_step_num), severity, priority, test_phase
    )

    # Jira credentials
    auth = get_auth()

    # Build payload
    payload = {
        "fields": {
            "project":   {"key": PROJECT_KEY},
            "issuetype": {"id": issue_type_id},  # safer than name
            "summary":   summary,
            "description": description_adf,

            # System field: Priority (by name is fine)
            "priority": {"name": priority},

            # Custom single-selects must be sent as {"id": "<optionId>"}
            SEVERITY_FIELD_ID:  {"id": severity_id},
            TEST_PHASE_FIELD_ID: {"id": test_phase_id},
        }
    }

    # Create issue
    create_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=payload,
        headers=headers_json(),
        auth=auth
    )

    if create_resp.status_code != 201:
        st.error("❌ Defect creation failed")
        st.code(create_resp.text)
        st.stop()

    issue_key = create_resp.json()["key"]
    st.success(f"✅ Defect created successfully: {issue_key}")

    # Link defect ↔ test ticket (Relates is symmetric; order does not matter)
    link_payload = {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": test_ticket.strip()},
        "outwardIssue": {"key": issue_key}
    }
    link_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issueLink",
        json=link_payload,
        headers=headers_json(),
        auth=auth
    )
    if link_resp.status_code not in (200, 201, 204):
        st.warning(f"Linking returned {link_resp.status_code}: {link_resp.text[:300]}")

    # Attach files
    if uploaded_files:
        for file in uploaded_files:
            attach_headers = {"X-Atlassian-Token": "no-check"}
            files = {"file": (file.name, file.getvalue())}
            attach_resp = requests.post(
                f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments",
                headers=attach_headers,
                files=files,
                auth=auth
            )
            if attach_resp.status_code not in (200, 201):
                st.warning(f"Attachment '{file.name}' failed: {attach_resp.status_code} {attach_resp.text[:200]}")

    st.success("📎 Attachments uploaded & Test Ticket linked")
    st.link_button("Open in Jira", f"{JIRA_BASE_URL}/browse/{issue_key}")
SEVERITY_FIELD_ID   = "customfield_10260"   # Severity  (single-select)

# ---------------------------
# STREAMLIT UI
# ---------------------------
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Create Jira Defect from Test Ticket")
st.markdown("**Fields marked with * are mandatory**")

test_ticket = st.text_input("Test Ticket Number * (e.g. CT-12345)", value="")
failed_step_num = st.number_input("Failed Test Step Number *", min_value=1, value=1, step=1)

