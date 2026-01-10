
# app.py
import base64, hashlib, hmac, json, os, time, urllib.parse
import requests
import streamlit as st
from requests.auth import HTTPBasicAuth

# -----------------------------
# Config & secrets
# -----------------------------
JIRA_BASE   = st.secrets["JIRA_BASE_URL"]
ZEPHYR_BASE     = "https://prod-api.zephyr4jiracloud.com/connect"
JIRA_EMAIL  = st.secrets["JIRA_EMAIL"]
ZEPHYR_ACCESS   = st.secrets["ZEPHYR_ACCESS_KEY"]
ZEPHYR_SECRET   = st.secrets["ZEPHYR_SECRET_KEY"]
ATL_ACCOUNT_ID  = st.secrets["ATLASSIAN_ACCOUNT_ID"]

GITHUB_TOKEN    = st.secrets.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")

st.set_page_config(page_title="Zephyr → GitHub Defect Logger", page_icon="🐞", layout="centered")
st.title("🐞 Zephyr Squad Cloud → GitHub Defect Logger")

# -----------------------------
# Helpers
# -----------------------------
def jira_issue_id_from_key(issue_key: str) -> str:
    """Convert a Jira issue key (e.g., CT-210789) to its internal numeric issueId."""
    url = f"{JIRA_BASE}/rest/api/3/issue/{issue_key}"
    r = requests.get(url,
                     headers={"Accept": "application/json"},
                     auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN))
    r.raise_for_status()
    return r.json()["id"]

def build_zephyr_jwt(method: str, relative_path: str, query_params: dict | None = None, expires_in: int = 360) -> str:
    """
    Build Zephyr Squad Cloud JWT.
    Docs require a per-request JWT with a query string hash (qsh):
    qsh = sha256("METHOD&path&canonical_query")
    Send headers:
      Authorization: JWT <token>
      zapiAccessKey: <access-key>
    """
    method = method.upper()
    qs = ""
    if query_params:
        qs = urllib.parse.urlencode(sorted(query_params.items()), doseq=True)

    canonical_request = f"{method}&{relative_path}&{qs}"
    qsh = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    now = int(time.time())
    payload = {
        "sub": ATL_ACCOUNT_ID,     # Atlassian Account ID
        "qsh": qsh,
        "iss": ZEPHYR_ACCESS,      # access key
        "exp": now + expires_in,
        "iat": now
    }
    header = {"typ": "JWT", "alg": "HS256"}

    def b64(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj, separators=(',', ':')).encode()
        ).rstrip(b'=')

    signing_input = b".".join([b64(header), b64(payload)])
    signature = base64.urlsafe_b64encode(
        hmac.new(ZEPHYR_SECRET.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b'=')

    return signing_input.decode() + "." + signature.decode()

def zephyr_get(relative_path: str, query_params: dict | None = None):
    """GET wrapper for Zephyr Cloud with JWT + access key headers."""
    query_params = query_params or {}
    jwt = build_zephyr_jwt("GET", relative_path, query_params)
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": ZEPHYR_ACCESS,
        "Accept": "application/json"
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.get(url, headers=headers, params=query_params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr GET {relative_path} failed {r.status_code}: {r.text}")
    return r.json()

def steps_to_markdown_squad(steps: list[dict]) -> str:
    """Format Squad test steps (step/data/result) to Markdown."""
    if not steps:
        return "*No steps found*"
    lines = []
    for s in sorted(steps, key=lambda x: x.get("orderId", 0)):
        step = (s.get("step") or "").strip()
        data = (s.get("data") or "").strip()
        exp  = (s.get("result") or "").strip()
        lines.append(f"- **Step**: {step}\n  - **Data**: {data}\n  - **Expected**: {exp}")
    return "\n".join(lines)

def exec_results_to_markdown(rows: list[dict]) -> str:
    """Format execution step results (status/comment/expected) to Markdown."""
    if not rows:
        return "*No execution step results found*"
    lines = []
    for r in rows:
        status  = r.get("status", "UNEXECUTED")
        exp     = (r.get("expectedResult") or "").strip()
        comment = (r.get("comment") or "").strip()
        lines.append(f"- **Status**: {status}\n  - **Expected**: {exp}\n  - **Comment**: {comment}")
    return "\n".join(lines)

def create_github_issue(owner: str, repo: str, title: str, body: str) -> str:
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    r = requests.post(url, headers=headers, json={"title": title, "body": body}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub issue create failed {r.status_code}: {r.text}")
    return r.json()["html_url"]

# -----------------------------
# UI
# -----------------------------
st.markdown("**Step 1 — Enter Test identifiers**")

col1, col2 = st.columns(2)
with col1:
    jira_test_key = st.text_input("Jira Test Key", placeholder="e.g., CT-210789")
with col2:
    execution_id = st.text_input("Execution ID (optional)", placeholder="e.g., 6161317")

st.markdown("**Step 2 — Fetch Zephyr content**")

if st.button("Fetch Test Steps"):
    try:
        if not jira_test_key:
            st.error("Please enter a Jira Test Key.")
        else:
            issue_id = jira_issue_id_from_key(jira_test_key)  # numeric ID
            # Cloud teststep endpoint path (relative to /connect):
            rel = f"/public/rest/api/1.0/teststep/{issue_id}"
            steps = zephyr_get(rel)
            md = steps_to_markdown_squad(steps)
            st.session_state["steps_md"] = md
            st.success("Fetched test steps.")
            st.subheader("Design‑time Steps (Markdown)")
            st.code(md, language="markdown")
    except Exception as e:
        st.error(f"Failed to fetch steps: {e}")

if st.button("Fetch Execution Step Results"):
    try:
        if not execution_id:
            st.error("Please enter an Execution ID.")
        else:
            # Depending on API version, execution step results path may differ; this is the common pattern.
            rel = f"/public/rest/api/1.0/executionstepresult/{execution_id}"
            rows = zephyr_get(rel)
            md_exec = exec_results_to_markdown(rows)
            st.session_state["exec_md"] = md_exec
            st.success("Fetched execution step results.")
            st.subheader("Execution‑time Step Results (Markdown)")
            st.code(md_exec, language="markdown")
    except Exception as e:
        st.error(f"Failed to fetch execution step results: {e}")

st.markdown("**Step 3 — Create GitHub defect**")

gh_owner = st.text_input("GitHub Owner/Org", placeholder="e.g., your-org")
gh_repo  = st.text_input("GitHub Repo", placeholder="e.g., defects")
title    = st.text_input("Defect Title", placeholder="Short, action‑oriented summary")
extra    = st.text_area("Additional context", placeholder="Observed behavior, environment, logs…")

steps_md = st.session_state.get("steps_md", "")
exec_md  = st.session_state.get("exec_md", "")

if st.button("Create GitHub Issue"):
    try:
        if not all([gh_owner, gh_repo, title, GITHUB_TOKEN]):
            st.error("Owner, Repo, Title and GITHUB_TOKEN are required.")
        else:
            body = f"### Steps to reproduce (Zephyr: {jira_test_key})\n{steps_md or '*None*'}"
            if exec_md:
                body += f"\n\n### Execution evidence (Execution ID: {execution_id})\n{exec_md}"
            if extra:
                body += f"\n\n### Additional context\n{extra}"
            issue_url = create_github_issue(gh_owner, gh_repo, title, body)
            st.success(f"Issue created: {issue_url}")
    except Exception as e:
        st.error(f"Failed to create GitHub issue: {e}")

st.caption("Notes: Zephyr Cloud API requires per‑request JWT + zapiAccessKey; Jira REST is used only to translate issue key → internal issueId.")
``
JIRA_TOKEN  = st.secrets["JIRA_API_TOKEN"]

