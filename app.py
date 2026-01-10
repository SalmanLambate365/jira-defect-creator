
import streamlit as st
import requests
import base64

# ---------- Page config ----------
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Jira Defect Creator from Test Ticket")

# ---------- Secrets ----------
# Make sure these keys match your Streamlit Cloud Secrets page
REQUIRED_SECRETS = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "PROJECT_KEY"]
missing = [k for k in REQUIRED_SECRETS if k not in st.secrets]
if missing:
    st.error(f"Missing secrets: {', '.join(missing)}. Go to App settings → Secrets and add them.")
    st.stop()

JIRA_URL    = st.secrets["JIRA_BASE_URL"].rstrip("/")  # remove trailing slash if present
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
    "X-Atlassian-Token": "no-check",  # required for attachment uploads
}

# ---------- UI ----------
with st.form("defect_form", clear_on_submit=False):
    test_ticket = st.text_input("Test Ticket ID", placeholder="e.g., TEST-123")
    step_number = st.number_input("Failed Test Step Number", min_value=1, step=1)

    uploaded_files = st.file_uploader(
        "Upload evidence (screenshots / logs)",
        accept_multiple_files=True,
        type=["png", "jpg", "jpeg", "gif", "bmp", "pdf", "txt", "log"]
    )

    submit = st.form_submit_button("Create Defect")

# ---------- Helpers ----------
def build_issue_payload(test_key: str, failed_step_num: int) -> dict:
    """Builds a minimal Jira Bug payload with rich-text description."""
    summary = f"[{test_key}] Failed at Step {failed_step_num}"
    description_text = (
        f"Test ticket: {test_key}\n\n"
        f"Failed step number: {failed_step_num}\n\n"
        "Please see attached evidences for details."
    )

    # Jira Cloud supports Atlassian Document Format; we’ll use simple text paragraph.
    body = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "summary": summary,
            "issuetype": {"name": "Defect"},
            "priority": {"name": "Medium"},  # default; adjust in UI if you want
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": description_text}
                        ]
                    }
                ]
            },
            # Optionally add labels/components/custom fields here
            # "labels": ["automation", "failed-step"],
        }
    }
    return body

def create_issue(payload: dict):
    url = f"{JIRA_URL}/rest/api/3/issue"
    return requests.post(url, headers=HEADERS_JSON, json=payload)

def link_issues(source_key: str, defect_key: str):
    """Link the test ticket to the newly created defect."""
    url = f"{JIRA_URL}/rest/api/3/issueLink"
    link_payload = {
        "type": {"name": "Relates"},  # change to your preferred link type if needed
        "inwardIssue": {"key": source_key},
        "outwardIssue": {"key": defect_key},
    }
    return requests.post(url, headers=HEADERS_JSON, json=link_payload)

def attach_files(issue_key: str, files):
    """Attach multiple uploaded evidence files to the created issue."""
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/attachments"
    results = []
    for f in files or []:
        # Streamlit UploadedFile supports .name and .getvalue()
        resp = requests.post(
            url,
            headers=HEADERS_ATTACH,
            files={"file": (f.name, f.getvalue())}
        )
        results.append((f.name, resp.status_code))
    return results

# ---------- Submit handling ----------
if submit:
    # Basic validation
    if not test_ticket.strip():
        st.error("Test Ticket ID is mandatory.")
        st.stop()
    if step_number is None or int(step_number) < 1:
        st.error("Please provide a valid failed test step number (>= 1).")
        st.stop()

    with st.spinner("Creating Jira defect…"):
        payload = build_issue_payload(test_ticket.strip(), int(step_number))
        create_resp = create_issue(payload)

    if create_resp.status_code == 201:
        defect_key = create_resp.json().get("key", "")
        st.success(f"✅ Defect created: {defect_key}")

        # Link to the test ticket
        with st.spinner("Linking defect to test ticket…"):
            link_resp = link_issues(test_ticket.strip(), defect_key)

        if link_resp.status_code in (200, 201, 204):
            st.info("🔗 Defect linked to test ticket.")
        else:
            st.warning(f"Could not link issues. Status: {link_resp.status_code} • {link_resp.text}")

        # Attach evidences
        if uploaded_files:
            with st.spinner("Uploading evidences…"):
                attach_results = attach_files(defect_key, uploaded_files)

            ok_count = sum(1 for _, code in attach_results if code in (200, 201))
            fail_items = [name for name, code in attach_results if code not in (200, 201)]
            if ok_count:
                st.success(f"📎 {ok_count} evidence file(s) attached.")
            if fail_items:
                st.warning(f"Some attachments failed: {', '.join(fail_items)}")
        else:
            st.info("No evidences uploaded.")

        # Quick link to Jira
        st.link_button("Open defect in Jira", f"{JIRA_URL}/browse/{defect_key}")
    else:
        st.error(f"❌ Error creating defect ({create_resp.status_code}): {create_resp.text}")
