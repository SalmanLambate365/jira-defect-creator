
import streamlit as st
import requests
import base64

# ---------- Page config ----------
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Defect Logger")

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

# ---------- Helper: fetch valid issue types for the project ----------
@st.cache_data(ttl=300)
def get_issue_types_for_project(project_key: str):
    url = f"{JIRA_URL}/rest/api/3/issue/createmeta/{project_key}/issuetypes"
    resp = requests.get(url, headers={"Authorization": f"Basic {auth_b64}", "Accept": "application/json"})
    if resp.status_code != 200:
        return []
    data = resp.json()
    # Response is a page with an "issueTypes" array (Cloud v3)
    issue_types = data.get("issueTypes") or data.get("issuetypes") or []
    # Normalize to list of {id, name}
    return [{"id": it.get("id"), "name": it.get("name")} for it in issue_types if it.get("id") and it.get("name")]

issue_types = get_issue_types_for_project(PROJECT_KEY)

if not issue_types:
    description_text = (    st.error("Could not retrieve valid issue types for this project. Check your permissions or project key.")
        f"Test ticket: {test_key}\n\n"
        f"Failed step number: {failed_step_num}\n\n"
        "See attached evidences."
    )
    return {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "summary": summary,
            "issuetype": {"id": issue_type_id},  # use ID to avoid name/duplicate issues
            "priority": {"name": "Medium"},
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description_text}]
                    }
                ]
            },
        }
    }

def create_issue(payload: dict):
    url = f"{JIRA_URL}/rest/api/3/issue"
    return requests.post(url, headers=HEADERS_JSON, json=payload)

def link_issues(source_key: str, defect_key: str):
    url = f"{JIRA_URL}/rest/api/3/issueLink"
    link_payload = {
        "type": {"name": "Relates"},  # change if your project uses a specific link type
        "inwardIssue": {"key": source_key},
        "outwardIssue": {"key": defect_key},
    }
    return requests.post(url, headers=HEADERS_JSON, json=link_payload)

def attach_files(issue_key: str, files):
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/attachments"
    results = []
    for f in files or []:
        resp = requests.post(url, headers=HEADERS_ATTACH, files={"file": (f.name, f.getvalue())})
        results.append((f.name, resp.status_code, resp.text))
    return results

if submit:
    if not test_ticket.strip():
        st.error("Test Ticket ID is mandatory.")
        st.stop()
    if int(step_number) < 1:
        st.error("Please provide a valid failed test step number (>= 1).")
        st.stop()

    # Find selected issue type ID
    selected = next((it for it in issue_types if it["name"] == issue_type_name), None)
    if not selected:
        st.error("Selected issue type is not available for this project.")
        st.stop()

    with st.spinner("Creating Jira defect…"):
        payload = build_issue_payload(test_ticket.strip(), int(step_number), selected["id"])
        create_resp = create_issue(payload)

    if create_resp.status_code == 201:
        defect_key = create_resp.json().get("key", "")
        st.success(f"✅ Defect created: {defect_key}")

        with st.spinner("Linking defect to test ticket…"):
            link_resp = link_issues(test_ticket.strip(), defect_key)
        if link_resp.status_code in (200, 201, 204):
            st.info("🔗 Defect linked to test ticket.")
        else:
            st.warning(f"Could not link issues. Status: {link_resp.status_code} • {link_resp.text}")

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
    else:
        # Show server message to help diagnose (e.g., invalid issue type)
        st.error(f"❌ Error creating defect ({create_resp.status_code}): {create_resp.text}")
    st.stop()

# Prefer 'Bug' if present; otherwise first available
default_type_name = "Bug" if any(it["name"].lower() == "bug" for it in issue_types) else issue_types[0]["name"]

# ---------- UI ----------
with st.form("defect_form", clear_on_submit=False):
    test_ticket = st.text_input("Test Ticket ID", placeholder="e.g., CT-12345")
    step_number = st.number_input("Failed Test Step Number", min_value=1, step=1)

    # User can choose the issue type among allowed ones
    issue_type_name = st.selectbox(
        "Issue Type",
        options=[it["name"] for it in issue_types],
        index=[it["name"] for it in issue_types].index(default_type_name)
    )

    uploaded_files = st.file_uploader(
        "Upload evidence (screenshots / logs)",
        accept_multiple_files=True,
        type=["png", "jpg", "jpeg", "gif", "bmp", "pdf", "txt", "log"]
    )

    submit = st.form_submit_button("Create Defect")

def build_issue_payload(test_key: str, failed_step_num: int, issue_type_id: str) -> dict:
    summary = f"[{test_key}] Failed at Step {failed_step_num}"
