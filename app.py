import streamlit as st
import requests
import json
from requests.auth import HTTPBasicAuth

# ---------------- PAGE CONFIG ----------------
st.set_page_config(page_title="CT JIRA Defect Creator", layout="centered")
st.title("🐞 CT – JIRA Defect Creator")

# ---------------- JIRA CONFIG ----------------
JIRA_BASE_URL = st.secrets["JIRA_BASE_URL"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_API_TOKEN = st.secrets["JIRA_API_TOKEN"]

auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# ---------------- USER INPUT ----------------
st.subheader("Failed Test Details")

test_ticket = st.text_input("Test Ticket Number (CT-xxx)", placeholder="CT-123")
failed_step = st.text_input("Failed Test Step Number", placeholder="Step 3")

severity = st.selectbox(
    "Severity",
    ["Sev-1", "Sev-2", "Sev-3", "Sev-4"]
)

test_phase = st.selectbox(
    "Test Phase",
    ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"]
)

st.divider()

# ---------------- CREATE DEFECT ----------------
if st.button("Create Defect"):
    if not test_ticket:
        st.error("❌ Test Ticket Number is mandatory")
        st.stop()

    # ---- Fetch Test Ticket ----
    issue_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{test_ticket}"
    issue_resp = requests.get(issue_url, headers=headers, auth=auth)

    if issue_resp.status_code != 200:
        st.error("❌ Unable to fetch test ticket")
        st.code(issue_resp.text)
        st.stop()

    issue_data = issue_resp.json()
    fields = issue_data.get("fields", {})

    summary = fields.get("summary", "")
    description = fields.get("description", "")

    # ---- Build Defect Description ----
    defect_description = f"""
Test Ticket: {test_ticket}
Failed Step: {failed_step}
Severity: {severity}
Test Phase: {test_phase}

----------------------------------
Test Summary:
{summary}

Test Description:
{description}
"""

    # ---- Create Defect Payload ----
    defect_payload = {
        "fields": {
            "project": {"key": "CT"},
            "summary": f"[AUTO] Defect from {test_ticket} - Step {failed_step}",
            "description": defect_description,
            "issuetype": {"name": "Defect"}
        }
    }

    create_url = f"{JIRA_BASE_URL}/rest/api/3/issue"
    create_resp = requests.post(
        create_url,
        headers=headers,
        auth=auth,
        data=json.dumps(defect_payload)
    )

    if create_resp.status_code == 201:
        defect_key = create_resp.json()["key"]
        st.success(f"✅ Defect created successfully: {defect_key}")

        # ---- Link Defect to Test Ticket ----
        link_payload = {
            "type": {"name": "Relates"},
            "inwardIssue": {"key": defect_key},
            "outwardIssue": {"key": test_ticket}
        }

        link_url = f"{JIRA_BASE_URL}/rest/api/3/issueLink"
        requests.post(
            link_url,
            headers=headers,
            auth=auth,
            data=json.dumps(link_payload)
        )

        st.success("🔗 Defect linked to test ticket")

    else:
        st.error("❌ Defect creation failed")
        st.code(create_resp.text)
