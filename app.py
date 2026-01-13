
import streamlit as st
import requests
import json
import re

# --- Jira & Zephyr constants ---
JIRA_BASE_URL = "https://mandg.atlassian.net"
PROJECT_KEY = "CT"
ISSUE_TYPE_CANDIDATES = ["Defect", "Bug"]
TEST_PHASE_FIELD_ID = "customfield_10245"
SEVERITY_FIELD_ID = "customfield_10260"

# --- Existing helpers (auth, Jira, Zephyr) ---
# Assume your original functions: get_auth(), headers_json(), get_zephyr_steps_and_expected(), fetch_test_ticket_fields_and_text()
# and ADF builders: adf_paragraph(), adf_heading(), adf_bullet_list(), adf_ordered_list()

# --- AI-lite helpers ---
def normalize_step(s: str) -> str:
    s = s.strip()
    s = re.sub(r'^\s*(?:step\s*\d+[\)\:\-]\s*|\d+[\)\.\-]\s*)', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    return s

def ai_lite_draft(context: dict) -> dict:
    steps = [normalize_step(x) for x in (context.get("zephyr_steps") or []) if x and x.strip()]
        zexp = "; ".join([x.strip() for x in (context.get("zephyr_expected") or []) if x.strip()])    failed_n = int(context.get("failed_step_num") or 0)
        expected_txt = zexp or "(not provided)"

    actual_txt = actual_txt or "(not provided)"
    impact_txt = impact_txt or "(not provided)"

    base_desc = (context.get("test_issue_description") or "").strip()
    issue_desc = base_desc or f"Related Test Ticket: {context.get('test_key')}"

    if failed_n >= 1 and failed_n <= len(steps):
        steps[failed_n - 1] = f"[FAILED] {steps[failed_n - 1]}"

    missing = []
    if actual_txt == "(not provided)": missing.append("Actual results not captured")
    if impact_txt == "(not provided)": missing.append("Impact not described")
    if not steps: missing.append("Steps to reproduce missing from Zephyr")

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

# --- Convert AI JSON to ADF ---
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

def make_adf_from_ai(test_key: str, ai: dict, evidence_names: list[str]) -> dict:
    content = []
    content.append(adf_heading("Issue Description"))
    content.append(adf_paragraph(ai.get("issue_description", f"Related Test Ticket: {test_key}")))
    content.append(adf_heading("Steps to reproduce"))
    content.extend(adf_ordered_list(ai.get("steps_to_reproduce", [])) or [adf_paragraph("(not provided)")])
    content.append(adf_heading("Expected Results"))
    content.append(adf_paragraph(ai.get("expected_results", "(not provided)")))
    content.append(adf_heading("Actual results"))
    content.append(adf_paragraph(ai.get("actual_results", "(not provided)")))
    content.append(adf_heading("Impact"))
    content.append(adf_paragraph(ai.get("impact", "(not provided)")))
    if ai.get("missing_info"):
        content.append(adf_heading("AI Notes"))
        content.extend(adf_bullet_list(ai["missing_info"]))
    content.append(adf_heading("Evidences"))
    content.extend(adf_bullet_list(evidence_names) if evidence_names else [adf_paragraph("See attachments.")])
    return {"type": "doc", "version": 1, "content": content}

# --- Streamlit UI ---
st.set_page_config(page_title="Jira Defect Creator", layout="centered")
st.title("🐞 Create Jira Defect from Test Ticket")
st.markdown("**Fields marked with * are mandatory**")

test_ticket = st.text_input("Test Ticket Number * (e.g. CT-12345)", value="")
failed_step_num = st.number_input("Failed Test Step Number *", min_value=1, value=1, step=1)
severity = st.selectbox("Severity *", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
priority = st.selectbox("Priority *", ["Critical", "Major", "Medium", "Minor"])
test_phase = st.selectbox("Test Phase *", ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"])
uploaded_files = st.file_uploader("Attach Evidence (screenshots, logs)", accept_multiple_files=True)

# --- AI-lite toggle ---
use_ai = st.checkbox("Use AI-lite to draft description & steps (beta)", value=True)

if st.button("🧠 Generate Draft (AI-lite)"):
    if not test_ticket.strip():
        st.error("Enter the Test Ticket key first.")
        st.stop()

    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip())
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")

    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip())
    except Exception as e:
        copied = {"labels": [], "components": [], "fixVersions": [], "versions": [],
                  "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""}}
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
    st.success("AI-lite draft generated. Review and edit below.")

# --- Editable preview ---
if use_ai and "ai_out" in st.session_state:
    ai = st.session_state["ai_out"]
    ai["issue_description"] = st.text_area("Issue Description", ai.get("issue_description", ""), height=120)
    steps_txt = "\n".join(ai.get("steps_to_reproduce", []))
    steps_txt = st.text_area("Steps to reproduce (one per line)", steps_txt, height=150)
    ai["steps_to_reproduce"] = [s.strip() for s in steps_txt.splitlines() if s.strip()]
    ai["expected_results"] = st.text_area("Expected Results", ai.get("expected_results", ""), height=100)
    ai["actual_results"]   = st.text_area("Actual Results", ai.get("actual_results", ""), height=100)
    ai["impact"]           = st.text_area("Impact", ai.get("impact", ""), height=90)

# --- Create Defect ---
if st.button("🚀 Create Defect"):
    if not all([test_ticket.strip(), severity, priority, test_phase]):
        st.error("Please fill all mandatory fields (*)")
        st.stop()

    summary = f"[{test_ticket.strip()}] Failed at Step {int(failed_step_num)}"
    evidence_names = [f.name for f in (uploaded_files or [])]

    if use_ai and "ai_out" in st.session_state:
        description_adf = make_adf_from_ai(test_key=test_ticket.strip(), ai=st.session_state["ai_out"], evidence_names=evidence_names)
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

    # Proceed with Jira create logic (same as your original code)
    # ...
    expected_txt = (context.get("test_expected_results") or "").strip()
    actual_txt   = (context.get("test_actual_results") or "").strip()
    impact_txt   = (context.get("test_impact") or "").strip()

    if not expected_txt:
