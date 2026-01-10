import streamlit as st
import requests
import base64

st.set_page_config(page_title="Jira Defect Creator", layout="centered")

st.title("🐞 Jira Defect Creator from Test Ticket")

# ---- Secrets ----
JIRA_URL = st.secrets["JIRA_URL"]
JIRA_EMAIL = st.secrets["JIRA_EMAIL"]
JIRA_TOKEN = st.secrets["JIRA_TOKEN"]
PROJECT_KEY = st.secrets["PROJECT_KEY"]

auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
headers = {
    "Authorization": f"Basic {auth}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# ---- UI ----
test_ticket = st.text_input("Test Ticket ID (e.g. TEST-123)")
failed_step = st.text_area("Failed Test Step")
expected_result = st.text_area("Expected Result")
actual_result = st.text_area("Actual Result")

priority = st.selectbox("Priority", ["High", "Medium", "Low"])

uploaded_files = st.file_uploader(
    "Upload evidence (screenshots / logs)",
    accept_multiple_files=True
)

if st.button("Create Defect"):
    if not test_ticket or not failed_step:
        st.error("Test Ticket ID and Failed Step are mandatory")
    else:
        payload = {
            "fields": {
                "project": {"key": PROJECT_KEY},
                "summary": f"Failure in {test_ticket} – {failed_step[:50]}",
                "issuetype": {"name": "Bug"},
                "priority": {"name": priority},
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"""
Test Ticket: {test_ticket}

Failed Step:
{failed_step}

Expected Result:
{expected_result}

Actual Result:
{actual_result}
"""
                                }
                            ]
                        }
                    ]
                }
            }
        }

        response = requests.post(
            f"{JIRA_URL}/rest/api/3/issue",
            headers=headers,
            json=payload
        )

        if response.status_code == 201:
            defect_key = response.json()["key"]
            st.success(f"Defect created successfully: {defect_key}")

            # ---- Link defect to test ticket ----
            link_payload = {
                "type": {"name": "is caused by"},
                "inwardIssue": {"key": test_ticket},
                "outwardIssue": {"key": defect_key}
            }

            requests.post(
                f"{JIRA_URL}/rest/api/3/issueLink",
                headers=headers,
                json=link_payload
            )

            # ---- Attach files ----
            for file in uploaded_files:
                attach_headers = {
                    "Authorization": f"Basic {auth}",
                    "X-Atlassian-Token": "no-check"
                }
                requests.post(
                    f"{JIRA_URL}/rest/api/3/issue/{defect_key}/attachments",
                    headers=attach_headers,
                    files={"file": (file.name, file.getvalue())}
                )

            st.info("Defect linked to test ticket and evidence attached.")
        else:
            st.error(f"Error creating defect: {response.text}")
