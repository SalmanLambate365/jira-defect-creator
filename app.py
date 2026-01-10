
import streamlit as st
import requests
import base64

# ---------- Page config ----------
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Jira Defect Creator from Test Ticket")

# ---------- Secrets ----------
REQUIRED_SECRETS = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "PROJECT_KEY"]
missing = [k for k in REQUIRED_SECRETS if k not in st.secrets]
if missing:
    st.error(f"Missing secrets: {', '.join(missing)}. Go to App settings → Secrets and add them.")
    st.stop()

JIRA_URL    = st.secrets["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL  = st.secrets["JIRA_EMAIL"]
JIRA_TOKEN  = st.secrets["JIRA_API_TOKEN"]
PROJECT_KEY = st.secrets["PROJECT_KEY"]

# Allow overriding custom field IDs via secrets
CUSTOMFIELD_SEVERITY  = st.secrets.get("CUSTOMFIELD_SEVERITY", "customfield_10260")
CUSTOMFIELD_TESTPHASE = st.secrets.get("CUSTOMFIELD_TESTPHASE", "customfield_10245")

# ---------- Auth & headers ----------
auth_b64 = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
HEADERS_JSON = {
    "Authorization": f"Basic {auth_b64}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
HEADERS_ATTACH = {
    "Authorization": f"Basic {auth_b64}",
    "X-Atlassian-Token": "no-check",
}

# ---------- Jira helpers ----------
@st.cache_data(ttl=300)
def get_issue_types_for_project(project_key: str):
    """
    Returns list of {id,name} issue types allowed for creation in the given project.
    """
    url = f"{JIRA_URL}/rest/api/3/issue/createmeta/{project_key}/issuetypes"
    r = requests.get(url, headers={"Authorization": f"Basic {auth_b64}", "Accept": "application/json"})
    if r.status_code != 200:
        return [], (r.status_code, r.text)
    data = r.json()
    issue_types = data.get("issueTypes") or data.get("issuetypes") or []
    return [{"id": it.get("id"), "name": it.get("name")} for it in issue_types if it.get("id") and it.get("name")], None

@st.cache_data(ttl=300)
def get_create_fields_for_issue_type(project_key: str, issue_type_id: str):
    """
    Returns create-field metadata for project+issueType:
    GET /rest/api/3/issue/createmeta/{projectKey}/issuetypes/{issueTypeId}
    """
    url = f"{JIRA_URL}/rest/api/3/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
    r = requests.get(url, headers={"Authorization": f"Basic {auth_b64}", "Accept": "application/json"})
    if r.status_code != 200:
        return None, (r.status_code, r.text)
    return r.json(), None

def get_issue_summary(key: str):
    r = requests.get(f"{JIRA_URL}/rest/api/3/issue/{key}?fields=summary", headers=HEADERS_JSON)
    if r.status_code == 200:
        return r.json().get("fields", {}).get("summary"), None
    return None, (r.status_code, r.text)

def create_issue(payload: dict):
    return requests.post(f"{JIRA_URL}/rest/api/3/issue", headers=HEADERS_JSON, json=payload)

def link_issues(source_key: str, defect_key: str):
    link_payload = {"type": {"name": "Relates"}, "inwardIssue": {"key": source_key}, "outwardIssue": {"key": defect_key}}
    return requests.post(f"{JIRA_URL}/rest/api/3/issueLink", headers=HEADERS_JSON, json=link_payload)

def attach_files(issue_key: str, files):
    results = []
    for f in files or []:
        resp = requests.post(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}/attachments",
            headers=HEADERS_ATTACH,
            files={"file": (f.name, f.getvalue())}
        )
        results.append((f.name, resp.status_code, resp.text))
    return results

def prefer_bug(issue_types: list[dict]) -> dict:
    for it in issue_types:
        if it["name"].lower() == "bug":
            return it
    return issue_types[0]

def build_adf_description(test_key: str, failed_step_num: int) -> dict:
    text = f"Test ticket: {test_key}\n\nFailed step number: {failed_step_num}\n\nSee attached evidences."
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ]
    }

def extract_single_select_options(fields_meta: dict, field_id: str):
    """
    From fields metadata, pull allowedValues for a single-select field by its fieldId.
    Returns list of dicts: [{"id": "...", "value": "..."}], or [] if not found.
    """
    fields_array = fields_meta.get("fields", []) if fields_meta else []
    for f in fields_array:
        if f.get("fieldId") == field_id:
            allowed = f.get("allowedValues") or []
            return [{"id": av.get("id"), "value": av.get("value") or av.get("name")} for av in allowed if av.get("id")]
    return []

# ---------- Load metadata once ----------
with st.spinner("Loading project & issue type metadata…"):
    issue_types, it_err = get_issue_types_for_project(PROJECT_KEY)
if it_err:
    st.error(f"Cannot get issue types for project {PROJECT_KEY} ({it_err[0]}): {it_err[1][:300]}")
    st.stop()
if not issue_types:
    st.error(f"No issue types available for project {PROJECT_KEY}. Check permissions or configuration.")
    st.stop()

chosen_type = prefer_bug(issue_types)
issue_type_id = chosen_type["id"]

with st.spinner("Loading required fields for selected issue type…"):
    fields_meta, fm_err = get_create_fields_for_issue_type(PROJECT_KEY, issue_type_id)
if fm_err:
    st.error(f"Cannot get create-field metadata ({fm_err[0]}): {fm_err[1][:300]}")
    st.stop()

# Pull allowed values for Severity & Test Phase (single-select custom fields)
severity_options   = extract_single_select_options(fields_meta, CUSTOMFIELD_SEVERITY)
testphase_options  = extract_single_select_options(fields_meta, CUSTOMFIELD_TESTPHASE)

if not severity_options:
    st.warning(f"No allowed values returned for Severity ({CUSTOMFIELD_SEVERITY}). Check field configuration.")
if not testphase_options:
    st.warning(f"No allowed values returned for Test Phase ({CUSTOMFIELD_TESTPHASE}). Check field configuration.")

# ---------- UI ----------
with st.form("defect_form", clear_on_submit=False):
    st.subheader(f"Project: {PROJECT_KEY} • Issue Type: {chosen_type['name']}")
    test_ticket = st.text_input("Test Ticket ID", placeholder="e.g., CT-210789")
    step_number = st.number_input("Failed Test Step Number", min_value=1, step=1)

    # Severity dropdown (single select option)
    if severity_options:
        severity_labels = [o["value"] for o in severity_options]
        # Try to default to "Medium" if present
        default_idx = severity_labels.index("Medium") if "Medium" in severity_labels else 0
        severity_choice = st.selectbox("Severity", options=severity_labels, index=default_idx)
        severity_id = next((o["id"] for o in severity_options if o["value"] == severity_choice), None)
    else:
        severity_choice = st.text_input("Severity (no configured options found)", placeholder="e.g., Medium")
        severity_id = None  # cannot send raw text for single-select; will error

    # Test Phase dropdown (single select option)
    if testphase_options:
        testphase_labels = [o["value"] for o in testphase_options]
        testphase_choice = st.selectbox("Test Phase", options=testphase_labels, index=0)
        testphase_id = next((o["id"] for o in testphase_options if o["value"] == testphase_choice), None)
    else:
        testphase_choice = st.text_input("Test Phase (no configured options found)", placeholder="e.g., System Test")
        testphase_id = None

    uploaded_files = st.file_uploader(
        "Upload evidence (screenshots / logs)",
        accept_multiple_files=True,
        type=["png", "jpg", "jpeg", "gif", "bmp", "pdf", "txt", "log"]
    )

    submit = st.form_submit_button("Create Defect")

# ---------- Submit ----------
if submit:
    # Basic validation
    if not test_ticket.strip():
        st.error("Test Ticket ID is mandatory.")
        st.stop()
    if int(step_number) < 1:
        st.error("Please provide a valid failed test step number (>= 1).")
        st.stop()

    # Ensure we have IDs for single-select fields
    missing_cfg = []
    if severity_options and not severity_id:
        missing_cfg.append("Severity")
    if testphase_options and not testphase_id:
        missing_cfg.append("Test Phase")
    if missing_cfg:
        st.error(f"Missing option IDs for: {', '.join(missing_cfg)}. Check Jira field configuration.")
        st.stop()

    # Optional: verify the test ticket exists
    with st.spinner("Validating test ticket…"):
        summary, err = get_issue_summary(test_ticket.strip())
    if summary:
        st.info(f"Test ticket summary: {summary}")
    elif err:
        st.warning(f"Could not validate test ticket ({err[0]}). Proceeding. Details: {err[1][:250]}")

    # Build payload with Severity & Test Phase
    payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "summary": f"[{test_ticket.strip()}] Failed at Step {int(step_number)}",
            "issuetype": {"id": issue_type_id},     # use ID for reliability
            "description": build_adf_description(test_ticket.strip(), int(step_number)),
            # Optional: set a default priority if desired
            # "priority": {"name": "Medium"}
        }
    }

    # Inject custom single-select fields as {"id": "<optionId>"}
    if severity_id:
        payload["fields"][CUSTOMFIELD_SEVERITY] = {"id": severity_id}
    if testphase_id:
        payload["fields"][CUSTOMFIELD_TESTPHASE] = {"id": testphase_id}

    # Create issue
    with st.spinner("Creating Jira defect…"):
        create_resp = create_issue(payload)

    if create_resp.status_code != 201:
        st.error(f"❌ Error creating defect ({create_resp.status_code}): {create_resp.text[:800]}")
        st.stop()

    defect_key = create_resp.json().get("key", "")
    st.success(f"✅ Defect created: {defect_key}")

    # Link to test ticket
    with st.spinner("Linking defect to test ticket…"):
        link_resp = link_issues(test_ticket.strip(), defect_key)
    if link_resp.status_code in (200, 201, 204):
        st.info("🔗 Defect linked to test ticket.")
    else:
        st.warning(f"Could not link issues. Status: {link_resp.status_code} • {link_resp.text[:400]}")

    # Attach files
    if uploaded_files:
        with st.spinner("Uploading evidences…"):
            results = attach_files(defect_key, uploaded_files)
        ok = sum(1 for _, code, _ in results if code in (200, 201))
        fail = [(name, code) for name, code, _ in results if code not in (200, 201)]
        if ok:
            st.success(f"📎 {ok} evidence file(s) attached.")
        if fail:
            st.warning(f"Some attachments failed: {fail}")
    else:
        st.info("No evidences uploaded.")

    st.link_button("Open defect in Jira", f"{JIRA_URL}/browse/{defect_key}")
