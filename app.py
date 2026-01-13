
# app.py
# --------------------------------------------------------------------------------
# Jira Defect Creator (background Zephyr fetch + copy fields from Test)
# + AI-lite mode to auto-draft Issue Description, Steps, Expected/Actual, Impact
# --------------------------------------------------------------------------------
# Prereqs:
# - .streamlit/secrets.toml with:
#   JIRA_EMAIL, JIRA_API_TOKEN,
#   ZEPHYR_ACCESS_KEY, ZEPHYR_SECRET_KEY, ATLASSIAN_ACCOUNT_ID
# - pip install streamlit requests
# --------------------------------------------------------------------------------

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import re
import streamlit as st
import requests
from requests.auth import HTTPBasicAuth

# ============================================================
# CONFIGURATION
# ============================================================
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"
ISSUE_TYPE_CANDIDATES = ["Defect", "Bug"]

# Known single-select custom field IDs in your Jira
TEST_PHASE_FIELD_ID = "customfield_10245"  # Test Phase
SEVERITY_FIELD_ID = "customfield_10260"    # Severity

# Resolve these by NAME → ID at runtime (case-insensitive)
CUST_TECH_PORTFOLIO_NAME = "Cust Tech Portfolio"
CUST_TECH_PRODUCT_NAME = "Cust Tech Product"
CUST_TECH_DELIVERY_TEAM_NAME = "Cust Tech Delivery Team"

# Names for description sections pulled from the Test ticket
EXPECTED_RESULTS_NAME = "Expected Results"
ACTUAL_RESULTS_NAME   = "Actual Results"
IMPACT_NAME           = "Impact"

# Zephyr Squad Cloud base
ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

# ============================================================
# BASIC HELPERS (AUTH / JIRA COMMON)
# ============================================================
def get_auth():
    try:
        return HTTPBasicAuth(st.secrets["JIRA_EMAIL"], st.secrets["JIRA_API_TOKEN"])
    except KeyError as e:
        st.error(f"Missing secret: {e}. Add in App → Settings → Secrets.")
        st.stop()

def headers_json():
    return {"Accept": "application/json", "Content-Type": "application/json"}

def jira_issue_id_from_key(issue_key: str) -> str:
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth(), timeout=30)
    r.raise_for_status()
    return r.json()["id"]

def jira_project_id_from_key(project_key: str) -> int:
    url = f"{JIRA_BASE_URL}/rest/api/3/project/{project_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth(), timeout=30)
    r.raise_for_status()
    return int(r.json()["id"])

# ---- Field ID cache & resolver ----
_FIELD_ID_CACHE: dict[str, str] = {}

def get_field_id_by_name(field_name: str) -> str | None:
    """Resolve Jira field ID by display name (case-insensitive)."""
    if field_name in _FIELD_ID_CACHE:
        return _FIELD_ID_CACHE[field_name]
    url = f"{JIRA_BASE_URL}/rest/api/3/field"
        if (f.get("name") or "").strip().lower() == field_name.strip().lower():    r = requests.get(url, headers={"Accept": "application/json"}, auth=get_auth(), timeout=60)
            _FIELD_ID_CACHE[field_name] = f["id"]
            return f["id"]
    return None

# ============================================================
# ZEPHYR (JWT + GET TEST STEPS)
# ============================================================
def build_zephyr_jwt(method: str, relative_path: str, query_params=None, expires_in: int = 360) -> str:
    method = method.upper()
    query_params = query_params or {}
    canonical_qs = urllib.parse.urlencode(sorted(query_params.items()), doseq=True)
    canonical_req = f"{method}&{relative_path}&{canonical_qs}"
    qsh = hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()
    now = int(time.time())
    payload = {
        "sub": st.secrets["ATLASSIAN_ACCOUNT_ID"],
        "qsh": qsh,
        "iss": st.secrets["ZEPHYR_ACCESS_KEY"],
        "exp": now + expires_in,
        "iat": now
    }
    header = {"typ": "JWT", "alg": "HS256"}

    def b64(obj: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")

    signing_input = b".".join([b64(header), b64(payload)])
    signature = base64.urlsafe_b64encode(
        hmac.new(st.secrets["ZEPHYR_SECRET_KEY"].encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return signing_input.decode() + "." + signature.decode()

def zephyr_get(relative_path: str, query_params=None):
    query_params = query_params or {}
    jwt = build_zephyr_jwt("GET", relative_path, query_params)
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": st.secrets["ZEPHYR_ACCESS_KEY"],
        "Accept": "application/json"
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.get(url, headers=headers, params=query_params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr GET {relative_path} failed {r.status_code}: {r.text[:300]}")
    return r.json()

def get_zephyr_steps_and_expected(jira_test_key: str) -> tuple[list[str], list[str]]:
    """
    Returns ordered step descriptions and expected results for Steps to reproduce/Expected section.
    """
    issue_id = jira_issue_id_from_key(jira_test_key)
    project_id = jira_project_id_from_key(PROJECT_KEY)
    rel = f"/public/rest/api/1.0/teststep/{issue_id}"
    rows = zephyr_get(rel, query_params={"projectId": project_id})
    steps, expected = [], []
    for s in sorted(rows, key=lambda x: x.get("orderId", 0)):
        step_txt = (s.get("step") or "").strip()
        exp_txt = (s.get("result") or "").strip()
        if step_txt:
            steps.append(step_txt)
        if exp_txt:
            expected.append(exp_txt)
    return steps, expected

# ============================================================
# FETCH FIELDS FROM TEST TICKET → TEMP STORE
# ============================================================
def fetch_test_ticket_fields_and_text(test_key: str) -> dict:
    """
    Pull labels/components/fixVersions/versions and custom fields by name,
    plus the Test ticket's **description** and the 3 text fields (Expected/Actual/Impact).
    """
    # resolve custom ids by name
    portfolio_id = get_field_id_by_name(CUST_TECH_PORTFOLIO_NAME)
    product_id   = get_field_id_by_name(CUST_TECH_PRODUCT_NAME)
    team_id      = get_field_id_by_name(CUST_TECH_DELIVERY_TEAM_NAME)
    expected_id  = get_field_id_by_name(EXPECTED_RESULTS_NAME)
    actual_id    = get_field_id_by_name(ACTUAL_RESULTS_NAME)
    impact_id    = get_field_id_by_name(IMPACT_NAME)

    standard = ["labels", "components", "fixVersions", "versions", "description"]
    custom_ids = [fid for fid in [portfolio_id, product_id, team_id, expected_id, actual_id, impact_id] if fid]
    fields_param = ",".join(standard + custom_ids)

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{test_key}"
    r = requests.get(
        url,
        params={"fields": fields_param},
        headers={"Accept": "application/json"},
        auth=get_auth(),
        timeout=30
    )
    r.raise_for_status()
    f = r.json().get("fields", {})

    # Normalize/shape values
    result = {
        "labels": f.get("labels") or [],
        "components": [{"name": c.get("name")} for c in (f.get("components") or [])],
        "fixVersions": [{"name": v.get("name")} for v in (f.get("fixVersions") or [])],
        "versions": [{"name": v.get("name")} for v in (f.get("versions") or [])],
        "custom": {},
        "text": {
            "issue_description": "",  # will render in ADF
            "expected_results": "",
            "actual_results": "",
            "impact": ""
        }
    }

    # description can be string or ADF doc depending on your tenant/screens
    desc = f.get("description")
    if isinstance(desc, dict) and desc.get("type") == "doc":
        # very simple ADF -> plain text extraction (first paragraph)
        try:
            paras = [n for n in desc.get("content", []) if n.get("type") == "paragraph"]
            result["text"]["issue_description"] = "".join(
                t.get("text", "") for t in paras[0].get("content", []) if t.get("type") == "text"
            ) if paras else ""
        except Exception:
            result["text"]["issue_description"] = ""
    else:
        result["text"]["issue_description"] = (desc or "").strip() if isinstance(desc, str) else ""

    def _copy_single_select(field_id):
        val = f.get(field_id)
        if isinstance(val, dict) and val.get("id"):
            result["custom"][field_id] = {"id": val["id"]}
        elif val:  # fallback if app returns a string/object
            result["custom"][field_id] = val

    # custom single-selects
    if portfolio_id: _copy_single_select(portfolio_id)
    if product_id:   _copy_single_select(product_id)
    if team_id:      _copy_single_select(team_id)

    # text fields — Expected/Actual/Impact
    def _get_text(field_id):
        v = f.get(field_id)
        if isinstance(v, dict) and v.get("type") == "doc":
            try:
                paras = [n for n in v.get("content", []) if n.get("type") == "paragraph"]
                return "".join(t.get("text", "") for t in paras[0].get("content", []) if t.get("type") == "text") if paras else ""
            except Exception:
                return ""
        return (v or "").strip() if isinstance(v, str) else ""

    if expected_id: result["text"]["expected_results"] = _get_text(expected_id)
    if actual_id:   result["text"]["actual_results"]   = _get_text(actual_id)
    if impact_id:   result["text"]["impact"]           = _get_text(impact_id)

    return result

# ============================================================
# ADF BUILDERS (sections as requested)
# ============================================================
def adf_text(text: str, marks: list | None = None):
    node = {"type": "text", "text": text}
    if marks: node["marks"] = marks
    return node

def adf_paragraph(text: str):
    return {"type": "paragraph", "content": [adf_text(text)]}

def adf_heading(text: str, level: int = 2):
    return {"type": "heading", "attrs": {"level": level}, "content": [adf_text(text)]}

def adf_bullet_list(items: list[str]):
    if not items: return []
    return [{"type": "bulletList",
             "content": [{"type": "listItem","content":[adf_paragraph(i)]} for i in items]}]

def adf_ordered_list(items: list[str]):
    if not items: return []
    return [{"type": "orderedList",
             "content": [{"type": "listItem","content":[adf_paragraph(i)]} for i in items]}]

def make_adf_description_from_sources(
    test_key: str,
    issue_description_txt: str,
    steps_list: list[str],
    expected_results_txt: str | None,
    expected_from_zephyr: list[str],
    actual_results_txt: str | None,
    impact_txt: str | None,
    evidence_names: list[str]
) -> dict:
    """
    Build ADF sections from fetched data:
    - Issue Description (from test ticket description)
    - Steps to reproduce (from Zephyr steps)
    - Expected Results (from test ticket field; else Zephyr expected)
    - Actual results (from test ticket field)
    - Impact (from test ticket field)
    - Evidences (file names)
    """
    content = []

    # 1) Issue Description
    content.append(adf_heading("Issue Description"))
    content.append(adf_paragraph((issue_description_txt or f"Related Test Ticket: {test_key}").strip()))

    # 2) Steps to reproduce
    content.append(adf_heading("Steps to reproduce"))
    if steps_list:
        content.extend(adf_ordered_list(steps_list))
    else:
        content.append(adf_paragraph("Steps not available from Zephyr."))

    # 3) Expected Results
    content.append(adf_heading("Expected Results"))
    if (expected_results_txt or "").strip():
        content.append(adf_paragraph(expected_results_txt.strip()))
    elif expected_from_zephyr:
        content.extend(adf_bullet_list(expected_from_zephyr))
    else:
        content.append(adf_paragraph("(not provided)"))

    # 4) Actual results
    content.append(adf_heading("Actual results"))
    content.append(adf_paragraph((actual_results_txt or "(not provided)").strip()))

    # 5) Impact
    content.append(adf_heading("Impact"))
    content.append(adf_paragraph((impact_txt or "(not provided)").strip()))

    # 6) Evidences
    content.append(adf_heading("Evidences"))
    if evidence_names:
        content.extend(adf_bullet_list(evidence_names))
    else:
        content.append(adf_paragraph("See attachments."))

    return {"type": "doc", "version": 1, "content": content}

# ============================================================
# AI-LITE (NO EXTERNAL MODEL) HELPERS — BOUND TO FAILED STEP
# ============================================================
def normalize_step(s: str) -> str:
    """Basic cleanup: remove numbering, collapse whitespace, keep imperative style if possible."""
    s = s.strip()
    # Remove numbering like "1) ", "Step 1 - ", "1. "
    s = re.sub(r'^\s*(?:step\s*\d+[\)\:\-]\s*|\d+[\)\.\-]\s*)', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    return s

def ai_lite_draft(context: dict) -> dict:
    """
    Deterministic drafting: bind Issue Description & Expected to the selected failed step,
    compose Actual & Impact conservatively if empty, mark failed step, add missing info hints.
    """
    raw_steps = context.get("zephyr_steps") or []
    zephyr_expected = context.get("zephyr_expected") or []
    steps = [normalize_step(x) for x in raw_steps if x and x.strip()]

    failed_n = int(context.get("failed_step_num") or 0)
    idx = failed_n - 1 if failed_n >= 1 else None
    selected_step = steps[idx] if (idx is not None and idx < len(steps)) else None

    # Expected Results: prefer Test field; else expected for THIS step; else all Zephyr expected; else not provided
    expected_txt = (context.get("test_expected_results") or "").strip()
    if not expected_txt:
        if idx is not None and idx < len(zephyr_expected):
            expected_txt = (zephyr_expected[idx] or "").strip() or "(not provided)"
        else:
            zexp_join = "; ".join([x.strip() for x in zephyr_expected if x.strip()])
            expected_txt = zexp_join or "(not provided)"

    # Actual Results: prefer Test field; else bind to failed step with attachments note
    actual_txt = (context.get("test_actual_results") or "").strip()
    if not actual_txt:
        if selected_step:
            actual_txt = f"Observed failure at step {failed_n}: {selected_step}. See attachments for details."
        else:
            actual_txt = "Observed failure during execution. See attachments for details."

    # Impact: prefer Test field; else derive from severity/test phase and failed step presence
    impact_txt = (context.get("test_impact") or "").strip()
    if not impact_txt:
        sev = (context.get("severity") or "").strip()
        phase = (context.get("test_phase") or "").strip()
        base_impact = "Failure prevents completion of the test scenario"
        if selected_step:
            base_impact = f"Failure at step {failed_n} ('{selected_step}') prevents completion of the scenario"
        sev_hint = {
            "Sev-1": "Critical user impact; workaround unlikely.",
            "Sev-2": "High impact; affects major user flows.",
            "Sev-3": "Moderate impact; limited scope.",
            "Sev-4": "Low impact; minor inconvenience."
        }.get(sev, "")
        phase_hint = f" Detected in {phase}." if phase else ""
        impact_txt = (base_impact + "." + phase_hint + (" " + sev_hint if sev_hint else "")).strip()

    # Issue Description: bind to failed step if present, else fallback to test description
    base_desc = (context.get("test_issue_description") or "").strip()
    if selected_step:
        issue_desc = f"Test {context.get('test_key')} failed at step {failed_n}: {selected_step}."
    else:
        issue_desc = base_desc or f"Failure reported in Test {context.get('test_key')}."

    # Mark failed step in steps list for visibility
    if idx is not None and idx < len(steps):
        steps[idx] = f"[FAILED] {steps[idx]}"

    # Missing info hints
    missing = []
    if not context.get("zephyr_steps"):
        missing.append("Steps to reproduce missing from Zephyr")
    if not (context.get("test_actual_results") or "").strip():
        missing.append("Actual results not captured in Test ticket")
    if not (context.get("test_impact") or "").strip():
        missing.append("Impact not described in Test ticket")

    return {
        "issue_description": issue_desc,
        "steps_to_reproduce": steps,
        "expected_results": expected_txt,
        "actual_results": actual_txt,
        "impact": impact_txt,
        "assumptions": [],
        "missing_info": missing,
        "confidence": "n/a"
    }

def make_adf_from_ai(test_key: str, ai: dict, evidence_names: list[str]) -> dict:
    """Convert AI-lite JSON into your Jira ADF document."""
    content = []

    # 1) Issue Description
    content.append(adf_heading("Issue Description"))
    desc_txt = (ai.get("issue_description") or f"Related Test Ticket: {test_key}").strip()
    content.append(adf_paragraph(desc_txt))

    # 2) Steps to reproduce
    content.append(adf_heading("Steps to reproduce"))
    steps = ai.get("steps_to_reproduce") or []
    content.extend(adf_ordered_list(steps) if steps else [adf_paragraph("(not provided)")])

    # 3) Expected Results
    content.append(adf_heading("Expected Results"))
    expected = (ai.get("expected_results") or "").strip()
    content.append(adf_paragraph(expected if expected else "(not provided)"))

    # 4) Actual results
    content.append(adf_heading("Actual results"))
    actual = (ai.get("actual_results") or "").strip()
    content.append(adf_paragraph(actual if actual else "(not provided)"))

    # 5) Impact
    content.append(adf_heading("Impact"))
    impact = (ai.get("impact") or "").strip()
    content.append(adf_paragraph(impact if impact else "(not provided)"))

    # Optional transparency
    assumptions = ai.get("assumptions") or []
    missing = ai.get("missing_info") or []
    if assumptions or missing:
        content.append(adf_heading("AI Notes"))
        notes = []
        if assumptions: notes.append("Assumptions: " + "; ".join(assumptions))
        if missing: notes.append("Missing info: " + "; ".join(missing))
        content.extend(adf_bullet_list(notes))

    # 6) Evidences
    content.append(adf_heading("Evidences"))
    content.extend(adf_bullet_list(evidence_names) if evidence_names else [adf_paragraph("See attachments.")])

    return {"type": "doc", "version": 1, "content": content}

# ============================================================
# STREAMLIT UI
# ============================================================
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Create Jira Defect from Test Ticket")
st.markdown("**Fields marked with * are mandatory**")

test_ticket = st.text_input("Test Ticket Number * (e.g. CT-12345)", value="")
failed_step_num = st.number_input("Failed Test Step Number *", min_value=1, value=1, step=1)
severity = st.selectbox("Severity *", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
priority = st.selectbox("Priority *", ["Critical", "Major", "Medium", "Minor"])
test_phase = st.selectbox("Test Phase *", ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"])
uploaded_files = st.file_uploader("Attach Evidence (screenshots, logs)", accept_multiple_files=True)

# ============================================================
# AI-LITE GENERATION
# ============================================================
use_ai = st.checkbox("Use AI-lite to draft description & steps (beta)", value=True)

if st.button("🧠 Generate Draft (AI-lite)"):
    if not test_ticket.strip():
        st.error("Enter the Test Ticket key first.")
        st.stop()

    # Background pulls
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip())
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")

    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip())
    except Exception as e:
        copied = {
            "labels": [], "components": [], "fixVersions": [], "versions": [],
            "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""}
        }
        st.warning(f"Could not fetch fields/description from Test: {e}")

    evidence_names = [f.name for f in (uploaded_files or [])]

    ctx = {
        "project_key": PROJECT_KEY,
        "test_key": test_ticket.strip(),
        "failed_step_num": int(failed_step_num),
        "severity": severity,
        "priority": priority,
        "test_phase": test_phase,
        "zephyr_steps": steps_list,
        "zephyr_expected": expected_from_zephyr,
        "test_issue_description": copied["text"]["issue_description"],
        "test_expected_results": copied["text"]["expected_results"],
        "test_actual_results": copied["text"]["actual_results"],
        "test_impact": copied["text"]["impact"],
        "labels": copied.get("labels", []),
        "components": copied.get("components", []),
        "versions": copied.get("versions", []),
        "fixVersions": copied.get("fixVersions", []),
        "evidence_names": evidence_names
    }

    ai_out = ai_lite_draft(ctx)
    st.session_state["ai_out"] = ai_out
    st.session_state["ctx"] = ctx  # keep context for Create if needed
    st.success("AI-lite draft generated. Review and edit below.")

# Editable preview
if use_ai and "ai_out" in st.session_state:
    ai = st.session_state["ai_out"]
    ai["issue_description"] = st.text_area("Issue Description", ai.get("issue_description", ""), height=120)
    steps_txt = "\n".join(ai.get("steps_to_reproduce", []))
    steps_txt = st.text_area("Steps to reproduce (one per line)", steps_txt, height=150)
    ai["steps_to_reproduce"] = [s.strip() for s in steps_txt.splitlines() if s.strip()]
    ai["expected_results"] = st.text_area("Expected Results", ai.get("expected_results", ""), height=100)
    ai["actual_results"]   = st.text_area("Actual Results", ai.get("actual_results", ""), height=100)
    ai["impact"]           = st.text_area("Impact", ai.get("impact", ""), height=90)

# ============================================================
# CREATE DEFECT
# ============================================================
if st.button("🚀 Create Defect"):
    if not all([test_ticket.strip(), severity, priority, test_phase]):
        st.error("Please fill all mandatory fields (*)")
        st.stop()

    # Resolve issue type & field meta
    url_it = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes"
    r_it = requests.get(url_it, headers={"Accept":"application/json"}, auth=get_auth(), timeout=30)
    if r_it.status_code != 200:
        st.error(f"Failed to fetch issue types: {r_it.status_code} {r_it.text[:300]}")
        st.stop()

    # prefer desired types
    it_by_name = {it.get("name"): it.get("id") for it in (r_it.json().get("issueTypes") or []) if it.get("id")}
    issue_type_id = next((it_by_name[n] for n in ISSUE_TYPE_CANDIDATES if n in it_by_name), None) or (r_it.json().get("issueTypes") or [{}])[0].get("id")

    fields_meta = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes/{issue_type_id}",
        headers={"Accept":"application/json"},
        auth=get_auth(),
        timeout=30
    ).json()

    def get_option_id(fields_meta: dict, field_id: str, chosen_label: str) -> str | None:
        for f in fields_meta.get("fields", []):
            if f.get("fieldId") == field_id:
                for opt in (f.get("allowedValues") or []):
                    label = opt.get("value") or opt.get("name")
                    if label == chosen_label:
                        return opt.get("id")
        return None

    severity_id   = get_option_id(fields_meta, SEVERITY_FIELD_ID, severity)
    test_phase_id = get_option_id(fields_meta, TEST_PHASE_FIELD_ID, test_phase)
    if not severity_id or not test_phase_id:
        st.error("Severity or Test Phase option not found on create screen.")
        st.stop()

    # Background pulls (reuse or refetch if needed)
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip())
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")

    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip())
    except Exception as e:
        copied = {
            "labels": [], "components": [], "fixVersions": [], "versions": [],
            "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""}
        }
        st.warning(f"Could not fetch fields/description from Test: {e}")

    evidence_names = [f.name for f in (uploaded_files or [])]
    summary = f"[{test_ticket.strip()}] Failed at Step {int(failed_step_num)}"

    # If AI-lite is enabled but user didn't click "Generate", auto-generate now
    ctx = st.session_state.get("ctx") or {
        "project_key": PROJECT_KEY,
        "test_key": test_ticket.strip(),
        "failed_step_num": int(failed_step_num),
        "severity": severity,
        "priority": priority,
        "test_phase": test_phase,
        "zephyr_steps": steps_list,
        "zephyr_expected": expected_from_zephyr,
        "test_issue_description": copied["text"]["issue_description"],
        "test_expected_results": copied["text"]["expected_results"],
        "test_actual_results": copied["text"]["actual_results"],
        "test_impact": copied["text"]["impact"],
        "labels": copied.get("labels", []),
        "components": copied.get("components", []),
        "versions": copied.get("versions", []),
        "fixVersions": copied.get("fixVersions", []),
        "evidence_names": evidence_names
    }
    if use_ai and "ai_out" not in st.session_state:
        st.session_state["ai_out"] = ai_lite_draft(ctx)

    # Choose description builder
    if use_ai and "ai_out" in st.session_state:
        description_adf = make_adf_from_ai(
            test_key=test_ticket.strip(),
            ai=st.session_state["ai_out"],
            evidence_names=evidence_names
        )
    else:
        description_adf = make_adf_description_from_sources(
            test_key=test_ticket.strip(),
            issue_description_txt=copied["text"]["issue_description"],
            steps_list=steps_list,
            expected_results_txt=copied["text"]["expected_results"],
            expected_from_zephyr=expected_from_zephyr,
            actual_results_txt=copied["text"]["actual_results"],
            impact_txt=copied["text"]["impact"],
            evidence_names=evidence_names
        )

    auth = get_auth()
    create_payload = {
        "fields": {
            "project": {"key": PROJECT_KEY},
            "issuetype": {"id": issue_type_id},
            "summary": summary,
            "description": description_adf,
            "priority": {"name": priority},
            SEVERITY_FIELD_ID: {"id": severity_id},
            TEST_PHASE_FIELD_ID: {"id": test_phase_id},
        }
    }

    # Create issue
    create_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=create_payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if create_resp.status_code != 201:
        st.error("❌ Defect creation failed")
        st.code(create_resp.text)
        st.stop()

    issue_key = create_resp.json()["key"]
    st.success(f"✅ Defect created: {issue_key}")

    # ---- Update copied fields on the new defect ----
    edit_fields = {}
    if copied["labels"]:      edit_fields["labels"] = copied["labels"]
    if copied["components"]:  edit_fields["components"] = copied["components"]
    if copied["fixVersions"]: edit_fields["fixVersions"] = copied["fixVersions"]
    if copied["versions"]:    edit_fields["versions"] = copied["versions"]

    # add custom Cust Tech fields
    for fid, val in copied["custom"].items():
        edit_fields[fid] = val

    if edit_fields:
        edit_resp = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}",
            json={"fields": edit_fields},
            headers=headers_json(),
            auth=auth,
            timeout=30
        )
        if edit_resp.status_code not in (200, 204):
            st.warning(f"Field copy update returned {edit_resp.status_code}: {edit_resp.text[:300]}")
        else:
            st.success("🔁 Copied Labels/Components/Versions/Custom fields from the Test ticket.")

    # Link defect ↔ test ticket
    link_payload = {"type": {"name": "Relates"}, "inwardIssue": {"key": test_ticket.strip()}, "outwardIssue": {"key": issue_key}}
    link_resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issueLink",
        json=link_payload,
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if link_resp.status_code not in (200, 201, 204):
        st.warning(f"Linking returned {link_resp.status_code}: {link_resp.text[:300]}")

    # Attach files
    if uploaded_files:
        for file in uploaded_files:
            attach_headers = {"X-Atlassian-Token": "no-check", "Accept": "application/json"}
            files = {"file": (file.name, file.getvalue())}
            attach_resp = requests.post(
                f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments",
                headers=attach_headers,
                files=files,
                auth=auth,
                timeout=60
            )
            if attach_resp.status_code not in (200, 201):
                st.warning(f"Attachment '{file.name}' failed: {attach_resp.status_code} {attach_resp.text[:200]}")

    st.success("📎 Attachments uploaded, fields synced & Test Ticket linked")
    st.link_button("Open in Jira", f"{JIRA_BASE_URL}/browse/{issue_key}")
    r.raise_for_status()
    for f in r.json():
