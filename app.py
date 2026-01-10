import streamlit as st
import requests
import base64

# ---------------- CONFIG ----------------
JIRA_BASE_URL = st.secrets["JIRA_BASE_URL"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_API_TOKEN = st.secrets["JIRA_API_TOKEN"]

PROJECT_KEY = "CT"
ISSUE_TYPE = "Defect"

auth_string = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}"
auth_bytes = base64.b64encode(auth_string.encode()).decode()

HEADERS = {
    "Authorization": f"Basic {auth_bytes}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# ---------------- HELPERS ----------------
def optional_list_field(value):
    return [{"name": value}] if value else []

def adf_text(text):
    return {
        "type": "doc",
        "version": 1,
        "content": [{
            "type": "paragraph",
            "content": [{
                "type": "text",
                "text": text
            }]
        }]
    }

def get_issue(issue_key):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers=HEADERS)
    return r.json() if r.status_code == 200 else None

# ---------------- UI ----------------
st.title("🐞 Defect Creator")

test_ticket = st.text_input("Test Ticket Number (e.g. CT-123)")
failed_step = st.text_input("Failed Test Step Number")

severity = st.selectbox(
    "Severity",
    ["Sev-1", "Sev-2", "Sev-3", "Sev-4"]
)

test_phase = st.selectbox(
    "Test Phase",
    ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"]
)

fix_version = st.text_input("Fix Version (optional)")
affected_version = st.text_input("Affected Version (optional)")
component = st.text_input("Component (optional)")

uploaded_files = st.file_uploader(
    "Attach Evidence (screenshots, logs)",
    accept_multiple_files=True
)

# ---------------- CREATE DEFECT ----------------
if st.button("Create Defect"):

    if not test_ticket or not failed_step:
        st.error("Test Ticket and Failed Step are mandatory")
        st.stop()

    test_issue = get_issue(test_ticket)

    summary = f"Defect from {test_ticket} – Failed Step {failed_step}"
    description_text = (
        f"Test Ticket: {test_ticket}\n"
        f"Failed Step: {failed_step}\n"
        f"Severity: {severity}\n"
        f"Test Phase: {test_phase}"
    )

    defect_payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "issuetype": {"name": ISSUE_TYPE},
            "summary": summary,
            "description": adf_text(description_text),
            "priority": {"name": severity},
            "components": optional_list_field(component),
            "fixVersions": optional_list_field(fix_version),
            "versions": optional_list_field(affected_version)
        }
    }

    create_url = f"{JIRA_BASE_URL}/rest/api/3/issue"
    response = requests.post(create_url, headers=HEADERS, json=defect_payload)

    if response.status_code != 201:
        st.error("❌ Defect creation failed")
        st.code(response.text)
        st.stop()

    defect_key = response.json()["key"]
    st.success(f"✅ Defect created: {defect_key}")

    # -------- Link Defect to Test Ticket --------
    link_payload = {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": defect_key},
        "outwardIssue": {"key": test_ticket}
    }

    link_url = f"{JIRA_BASE_URL}/rest/api/3/issueLink"
    requests.post(link_url, headers=HEADERS, json=link_payload)

    # -------- Upload Attachments --------
    if uploaded_files:
        attach_headers = {
            "Authorization": f"Basic {auth_bytes}",
            "X-Atlassian-Token": "no-check"
        }

        attach_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{defect_key}/attachments"

        for file in uploaded_files:
            files = {"file": (file.name, file, file.type)}
            requests.post(attach_url, headers=attach_headers, files=files)

    st.success("📎 Attachments uploaded & defect linked successfully")
