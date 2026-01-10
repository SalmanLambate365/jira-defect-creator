
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
    "X-Atlassian-Token": "no-check",  # required for attachments on Jira Cloud
}

# ---------- Helpers ----------
@st.cache_data(ttl=300)
def get_issue_types_for_project(project_key: str):
    """
    Fetch valid issue types for create in the given project.
    We use the Cloud REST v3 'create meta' endpoint:
    GET /rest/api/3/issue/createmeta/{projectKey}/issuetypes
    """
    url = f"{JIRA_URL}/rest/api/3/issue/createmeta/{project_key}/issuetypes"
    resp = requests.get(url, headers={"Authorization": f"Basic {auth_b64}", "Accept": "application/json"})
    if resp.status_code != 200:
        return [], (resp.status_code, resp.text)
    # Cloud v3 returns {"issueTypes":[{"id":"1","name":"Bug", ...}, ...]}
    data = resp.json()
    issue_types = data.get("issueTypes") or data.get("issuetypes") or []
    normalized = [{"id": it.get("id"), "name": it.get("name")} for it in issue_types if it.get("id") and it.get("name")]
    return normalized, None

def pick_issue_type_id(issue_types: list[dict]) -> str:
    """
    Prefer 'Bug' if present; otherwise the first available type.
    """
    for it in issue_types:
        if it["name"].lower() == "bug":
            return it["id"]
    return issue_types[0]["id"]

def build_issue_payload(test_key: str, failed_step_num: int, issue_type_id: str) -> dict:
    summary = f"[{test_key}] Failed at Step {failed_step_num}"
    description_text = (
        f"Test ticket: {test_key}\n\n"
        f"Failed step number: {failed_step_num}\n\n"
        "See attached evidences."
    )
    # Use Atlassian Document Format (ADF) for description
    return {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "summary": summary,
            "issuetype": {"id": issue_type_id},   # use ID to avoid 'invalid issue type' problems
            "priority": {"name": "Medium"},       # default; adjust if you want later
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
    """
    Link the test ticket to the created defect using a generic link type.
    Change 'Relates' if your project prefers a specific type (e.g., 'is caused by', 'blocks').
    """
    url = f"{JIRA_URL}/rest/api/3/issueLink"
    link_payload = {
        "type": {"name": "Relates"},
        "inwardIssue": {"key": source_key},
        "outwardIssue": {"key": defect_key},
    }
