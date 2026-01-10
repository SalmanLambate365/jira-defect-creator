import streamlit as st
import requests
import os
from requests.auth import HTTPBasicAuth

# ---------------------------
# CONFIGURATION
# ---------------------------
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"
ISSUE_TYPE = "Defect"

# 🔴 UPDATE THESE WITH REAL FIELD IDS
TEST_PHASE_FIELD_ID = "customfield_10245"   # Test Phase
SEVERITY_FIELD_ID = "customfield_10260"     # Severity

# ---------------------------
# STREAMLIT UI
# ---------------------------
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Create Jira Defect from Test Ticket")

st.markdown("Fields marked with * are mandatory")

test_ticket = st.text_input("Test Ticket Number * (e.g. CT-12345)")
failed_step = st.text_input("Failed Test Step Number *")


severity = st.selectbox(
    "Severity *",
    ["Sev-1", "Sev-2", "Sev-3", "Sev-4"]
)

priority = st.selectbox(
    "Priority *",
    ["Critical", "Major", "Medium", "Minor"]
)

test_phase = st.selectbox(
    "Test Phase *",
    ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"]
)

uploaded_files = st.file_uploader(
    "Attach Evidence (screenshots, logs)",
    accept_multiple_files=True
)

# ---------------------------
# CREATE DEFECT BUTTON
# ---------------------------
if st.button("🚀 Create Defect"):

    if not test_ticket or not failed_step :
        st.error("Please fill all mandatory fields (*)")
        st.stop()

    # Jira credentials from Streamlit secrets
    auth = HTTPBasicAuth(
        st.secrets["JIRA_EMAIL"],
        st.secrets["JIRA_API_TOKEN"]
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # ---------------------------
    # ADF DESCRIPTION (MANDATORY)
    # ---------------------------
    description_adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": f"Test Ticket: {test_ticket}"}
                ]
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": f"Failed Step: {failed_step}"}
                ]
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": description_text or ""}
                ]
            }
        ]
    }

    # ---------------------------
    # JIRA PAYLOAD
    # ---------------------------
    payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "issuetype": {"name": ISSUE_TYPE},
           
            "description": description_adf,

            # Priority (SYSTEM FIELD)
            "priority": {"name": priority},

            # Severity (CUSTOM FIELD)
            SEVERITY_FIELD_ID: {"value": severity},

            # Test Phase (REQUIRED CUSTOM FIELD)
            TEST_PHASE_FIELD_ID: {"value": test_phase}
        }
    }

    # ---------------------------
    # CREATE ISSUE
    # ---------------------------
    response = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=payload,
        headers=headers,
        auth=auth
    )

    if response.status_code != 201:
        st.error("❌ Defect creation failed")
        st.code(response.text)
        st.stop()

    issue_key = response.json()["key"]
    st.success(f"✅ Defect created successfully: {issue_key}")

    # ---------------------------
    # LINK DEFECT TO TEST TICKET
    # ---------------------------
    link_payload = {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": issue_key},
        "outwardIssue": {"key": test_ticket}
    }

    requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issueLink",
        json=link_payload,
        headers=headers,
        auth=auth
    )

    # ---------------------------
    # ATTACH FILES
    # ---------------------------
    if uploaded_files:
        for file in uploaded_files:
            attach_headers = {
                "X-Atlassian-Token": "no-check"
            }
            files = {"file": (file.name, file.getvalue())}

            requests.post(
                f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments",
                headers=attach_headers,
                files=files,
                auth=auth
            )

    st.success("📎 Attachments uploaded & Test Ticket linked")
