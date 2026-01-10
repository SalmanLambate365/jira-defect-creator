import streamlit as st
import requests
import base64

# ---------------- CONFIG ----------------
JIRA_BASE_URL = st.secrets["JIRA_BASE_URL"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_API_TOKEN = st.secrets["JIRA_API_TOKEN"]

PROJECT_KEY = "CT"
ISSUE_TYPE = "Defect"

SEVERITY_FIELD_ID = "customfield_10260"  # 🔴 replace with actual ID

# ---------------- AUTH ----------------
auth_string = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}"
auth_bytes = base64.b64encode(auth_string.encode()).decode()

HEADERS = {
    "Authorization": f"Basic {auth_bytes}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# ---------------- HELPERS ----------------
def adf_text(text):
    return {
        "type": "doc",
        "version": 1,
        "content": [{
            "type": "paragraph",
            "content": [{"type": "text", "text": text}]
        }]
    }

def optional_list_field(value):
    return [{"name": value}] if value else []

def map_severity_to_priority(severity):
    return {
        "Sev-1": "Blocker",
        "Sev-2": "Major",
        "Sev-3": "Medium",
        "Sev-4": "Minor"
    }.get(severity, "Medium")

# ---------------- UI ----------------
st.title("🐞 Defect Creation from Test Ticket")

test_ticket = st.text_input("Test Ticket Number (e.g. CT-19345)")
failed_step = st.text_input("Failed Step Number")

severity = st.selectbox(
    "Severity",
    ["Sev-1", "Sev-2", "Sev-3", "Sev-4"]
)

priority_preview = map_severity_to_priority(severity)
st.info(f"📌 Jira Priority will be set to **{priority_preview}**")

test_phase = st.selectbox(
    "Test Phase",
    ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"]
)

fix_version = st.text_input("Fix Version (optional)")
affected_version = st.text_input("Affected Version (optional)")
component = st.text_input("Component (optional)")

attachments = st.file_uploader(
    "Attach Evidence",
    accept_multiple_files=True
)

# ---------------- CREATE DEFECT ----------------
if st.button("Create Defect"):

    if not test_ticket or not failed_step:
        st.error("Test Ticket and Failed Step are mandatory")
        st.stop()

    description = (
        f"Test Ticket: {test_ticket}\n"
        f"Failed Step: {failed_step}\n"
        f"Severity: {severity}\n"
        f"Test Phase: {test_phase}"
    )

    defect_payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "issuetype": {"name": ISSUE_TYPE},
            "summary": f"Defect from {test_ticket} – Failed Step {failed_step}",
            "description": adf_text(description),

            # ✅ Priority (system field)
            "priority": {
                "name": map_severity_to_priority(severity)
            },

            # ✅ Severity (custom field)
            SEVERITY_FIELD_ID: {
                "value": severity
            },

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

    # -------- Attach files --------
    if attachments:
        attach_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{defect_key}/attachments"
        attach_headers = {
            "Authorization": f"Basic {auth_bytes}",
            "X-Atlassian-Token": "no-check"
        }

        for file in attachments:
            files = {"file": (file.name, file, file.type)}
            requests.post(attach_url, headers=attach_headers, files=files)

    st.success("🎉 Defect created with Priority & Severity correctly set")
