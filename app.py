
# app.py
# --------------------------------------------------------------------------------
# Jira Defect Creator (Create defect from Test; copy fields; Zephyr integration)
# --------------------------------------------------------------------------------
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import re
import io
import colorsys
from pathlib import Path
from PIL import Image

import streamlit as st
from streamlit.components.v1 import html as st_html
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
SEVERITY_FIELD_ID   = "customfield_10260"  # Severity

# Resolve these by NAME → ID at runtime (case-insensitive)
CUST_TECH_PORTFOLIO_NAME     = "Cust Tech Portfolio"
CUST_TECH_PRODUCT_NAME       = "Cust Tech Products"
CUST_TECH_DELIVERY_TEAM_NAME = "Cust Tech Delivery Teams"

# Names for description sections pulled from the Test ticket
EXPECTED_RESULTS_NAME = "Expected Results"
ACTUAL_RESULTS_NAME   = "Actual Results"
IMPACT_NAME           = "Impact"  # kept for fetch compatibility; not shown in UI

# Zephyr Squad Cloud base
ZEPHYR_BASE = "https://prod-api.zephyr4jiracloud.com/connect"

# Feature flag: some Cloud tenants don’t expose step results via API consistently.
# Toggle to True only if your tenant supports GET/PUT of execution step results.
ENABLE_STEP_UPDATE = True

# ============================================================
# BRANDING: Title bar (logo + title), green divider, fixed footer
# ============================================================
def _rgb_to_hex(rgb):
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"

def _dominant_palette(img: Image.Image, k: int = 8):
    """
    Returns a list of ((R,G,B), count) sorted by count desc using adaptive palette.
    """
    small = img.copy().convert("RGBA").resize((160, 160))
    pixels = [px for px in small.getdata() if px[3] > 0]
    if not pixels:
        return [((22, 163, 74), 1)]  # fallback green (#16A34A)

    pal_img = small.convert("P", palette=Image.ADAPTIVE, colors=k)
    palette = pal_img.getpalette()[:k * 3]
    color_counts = pal_img.getcolors() or []

    def idx_to_rgb(i):
        base = i * 3
        return (palette[base], palette[base + 1], palette[base + 2])

    colors = [(idx_to_rgb(i), c) for (c, i) in color_counts]
    colors.sort(key=lambda x: x[1], reverse=True)
    return colors

def _pick_brand_colors(img: Image.Image):
    """
    Heuristically pick:
      - brand_green: saturated green (for divider & footer border)
      - brand_teal : darker teal/blue-green (for top bar background)
    Fallbacks used if not found.
    """
    colors = _dominant_palette(img, k=8)

    def to_hsv(rgb):
        r, g, b = [v/255 for v in rgb]
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        return (h * 360.0, s, v)

    def green_score(rgb, count):
        h, s, v = to_hsv(rgb)
        hue_bias = max(0.0, 1.0 - abs(h - 140) / 60.0)  # favor ~140°
        return (s * 0.8 + v * 0.2) * hue_bias * (1 + count/1000.0)

    def teal_score(rgb, count):
        h, s, v = to_hsv(rgb)
        hue_bias = max(0.0, 1.0 - abs(h - 180) / 70.0)  # favor ~180°
        return (s * 0.6 + (1 - v) * 0.6) * hue_bias * (1 + count/1000.0)

    green_best, green_best_score = None, -1
    teal_best, teal_best_score   = None, -1
    for rgb, cnt in colors:
        gs = green_score(rgb, cnt)
        if gs > green_best_score:
            green_best, green_best_score = rgb, gs
        ts = teal_score(rgb, cnt)
        if ts > teal_best_score:
            teal_best, teal_best_score = rgb, ts

    if not green_best:
        green_best = (22, 163, 74)   # #16A34A
    if not teal_best:
        teal_best = (5, 68, 74)      # dark teal fallback

    return _rgb_to_hex(green_best), _rgb_to_hex(teal_best)


from streamlit.components.v1 import html as st_html
import base64, io
from pathlib import Path
from PIL import Image
import colorsys

def _rgb_to_hex(rgb):
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"

def _dominant_palette(img: Image.Image, k: int = 8):
    small = img.copy().convert("RGBA").resize((160, 160))
    pixels = [px for px in small.getdata() if px[3] > 0]
    if not pixels:
        return [((22, 163, 74), 1)]
    pal_img = small.convert("P", palette=Image.ADAPTIVE, colors=k)
    palette = pal_img.getpalette()[:k * 3]
    color_counts = pal_img.getcolors() or []
    def idx_to_rgb(i):
        base = i * 3
        return (palette[base], palette[base+1], palette[base+2])
    colors = [(idx_to_rgb(i), c) for (c, i) in color_counts]
    colors.sort(key=lambda x: x[1], reverse=True)
    return colors

def _pick_brand_colors(img: Image.Image):
    def to_hsv(rgb):
        r, g, b = [v/255 for v in rgb]
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        return (h*360.0, s, v)
    colors = _dominant_palette(img, k=8)
    def green_score(rgb, count):
        h, s, v = to_hsv(rgb)
        return (s*0.8 + v*0.2) * max(0.0, 1.0 - abs(h - 140)/60.0) * (1 + count/1000.0)
    def teal_score(rgb, count):
        h, s, v = to_hsv(rgb)
        return (s*0.6 + (1 - v)*0.6) * max(0.0, 1.0 - abs(h - 180)/70.0) * (1 + count/1000.0)
    green_best, gs_best = None, -1
    teal_best, ts_best   = None, -1
    for rgb, cnt in colors:
        gs = green_score(rgb, cnt)
        ts = teal_score(rgb, cnt)
        if gs > gs_best: green_best, gs_best = rgb, gs
        if ts > ts_best: teal_best, ts_best = rgb, ts
    if not green_best: green_best = (22, 163, 74)  # #16A34A
    if not teal_best:  teal_best  = (5, 68, 74)
    return _rgb_to_hex(green_best), _rgb_to_hex(teal_best)

def add_titlebar_branding(
    header_image_path: str,
    app_title: str = "🐞 AutoDefect Logger",
    app_subtitle: str | None = "Jira Defect Creator",
    footer_text: str = "AutoDefect Logger • © 2026",
    brand_green_hex: str | None = None,
    brand_teal_hex: str | None = None,
    logo_height_px: int = 48,
    logo_side: str = "right",
    max_inner_width_px: int = 1200
):
    """
    Renders title bar + green divider inside an iframe and injects the footer
    outside the iframe so it stays fixed to the page bottom.
    """
    logo_data_url = ""
    if Path(header_image_path).exists():
        img = Image.open(header_image_path).convert("RGB")
        auto_green, auto_teal = _pick_brand_colors(img)
        brand_green = brand_green_hex or auto_green
        brand_teal  = brand_teal_hex  or auto_teal
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        logo_data_url = f"data:image/png;base64,{b64}"
    else:
        brand_green = brand_green_hex or "#16A34A"
        brand_teal  = brand_teal_hex  or "#0f5b5f"
        logo_data_url = ""

    title_html = f"""
      <div class="mg-title">
        <div class="mg-title-line">{app_title}</div>
        {f"<div class='mg-subtitle'>{app_subtitle}</div>" if app_subtitle else ""}
      </div>
    """

    if logo_side.lower() == "left":
        left_html  = f"<div class='mg-logo-wrap'>{f'<img class=\"mg-logo\" src=\"{logo_data_url}\" alt=\"M&G Logo\" />' if logo_data_url else ''}</div>"
        right_html = f"<div class='mg-title-wrap'>{title_html}</div>"
    else:
        left_html  = f"<div class='mg-title-wrap'>{title_html}</div>"
        right_html = f"<div class='mg-logo-wrap'>{f'<img class=\"mg-logo\" src=\"{logo_data_url}\" alt=\"M&G Logo\" />' if logo_data_url else ''}</div>"

    # ---- TOP BAR + DIVIDER in iframe ----
    html_blob = f"""
      <style>
        :root {{
          --brand-green: {brand_green};
          --brand-teal:  {brand_teal};
          --title-fg:    #ffffff;
          --subtitle-fg: #e2e8f0;
        }}
        .mg-topbar-wrap {{
          width: 100%;
          background-color: var(--brand-teal);
          margin: 0;
          padding: 0;
        }}
        .mg-topbar {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          max-width: {max_inner_width_px}px;
          margin: 0 auto;
          padding: 10px 14px;
        }}
        .mg-title-wrap, .mg-logo-wrap {{ display:flex; align-items:center; }}
        .mg-title {{ display:flex; flex-direction:column; gap:2px; }}
        .mg-title-line {{
          color: var(--title-fg);
          font-weight: 700;
          font-size: 1.25rem;
          line-height: 1.2;
          letter-spacing: 0.2px;
        }}
        .mg-subtitle {{
          color: var(--subtitle-fg);
          font-weight: 500;
          font-size: 0.95rem;
          line-height: 1.2;
        }}
        .mg-logo {{
          height: {logo_height_px}px;
          width: auto;
          display: block;
          border-radius: 6px;
        }}
        .green-line {{
          height: 4px;
          background-color: var(--brand-green);
          border: none;
          margin: 0.25rem auto 1.25rem auto;
          max-width: {max_inner_width_px}px;
        }}
      </style>

      <div class="mg-topbar-wrap">
        <div class="mg-topbar">
          {left_html}
          {right_html}
        </div>
      </div>
      <div class="green-line"></div>
    """
    st_html(html_blob, height=120, scrolling=False)

    # ---- FOOTER in main page DOM (fixed to viewport bottom) ----
    st.markdown(f"""
        <style>
            .footer-fixed {{
                position: fixed;
                left: 0; right: 0; bottom: 0;
                width: 100%;
                background: #ffffff;
                border-top: 4px solid {brand_green};
                padding: 8px 16px;
                font-size: 0.9rem;
                color: #334155;
                z-index: 9999;
            }}
            .block-container {{ padding-bottom: 80px; }}
        </style>
        <div class="footer-fixed">{footer_text}</div>
    """, unsafe_allow_html=True)

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

def jira_issue_id_from_key(issue_key, auth):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    return r.json()["id"]

def jira_project_id_from_key(project_key, auth):
    url = f"{JIRA_BASE_URL}/rest/api/3/project/{project_key}"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    return int(r.json()["id"])

# -- Field ID cache & resolver --
_FIELD_ID_CACHE = {}
def get_field_id_by_name(field_name, auth):
    """
    Resolve Jira field ID by display name (case-insensitive).
    Returns the field ID or None if not found.
    """
    cached = _FIELD_ID_CACHE.get(field_name)
    if cached:
        return cached
    url = f"{JIRA_BASE_URL}/rest/api/3/field"
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=60)
    r.raise_for_status()
    for f in r.json():
        name = (f.get("name") or "").strip().lower()
        if name == field_name.strip().lower():
            fid = f.get("id")
            if fid:
                _FIELD_ID_CACHE[field_name] = fid
                return fid
    return None

# ============================================================
# ZEPHYR (JWT + HELPERS) — Cloud (JWT per request; QSH from path+query)
# ============================================================
def build_zephyr_jwt(method, relative_path, query_params=None, expires_in=360):
    """
    Generate Zephyr Squad Cloud JWT with 'qsh' = SHA256(METHOD & RELATIVE_PATH & sorted(querystring)).
    - relative_path: path beginning with '/public/rest/api/1.0/...'
    - query_params: dict; must match actual request; keys sorted
    """
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
    def b64(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj, separators=(",", ":")).encode()
        ).rstrip(b"=")
    signing_input = b".".join([b64(header), b64(payload)])
    signature = base64.urlsafe_b64encode(
        hmac.new(st.secrets["ZEPHYR_SECRET_KEY"].encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return signing_input.decode() + "." + signature.decode()

def zephyr_request(method, relative_path, query_params=None, json_body=None):
    method = method.upper()
    query_params = query_params or {}
    jwt = build_zephyr_jwt(method, relative_path, query_params)
    headers = {
        "Authorization": f"JWT {jwt}",
        "zapiAccessKey": st.secrets["ZEPHYR_ACCESS_KEY"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{ZEPHYR_BASE}{relative_path}"
    r = requests.request(method, url, headers=headers, params=query_params, json=json_body, timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"Zephyr {method} {relative_path} failed {r.status_code}: {r.text[:400]}")
    if r.text.strip():
        try:
            return r.json()
        except Exception:
            return r.text
    return None

def zephyr_get(relative_path, query_params=None):
    return zephyr_request("GET", relative_path, query_params=query_params)

def zephyr_post(relative_path, json_body=None, query_params=None):
    return zephyr_request("POST", relative_path, query_params=query_params, json_body=json_body)

def zephyr_put(relative_path, json_body=None, query_params=None):
    return zephyr_request("PUT", relative_path, query_params=query_params, json_body=json_body)

# ============================================================
# FETCH FIELDS FROM TEST TICKET → TEMP STORE
# ============================================================
def fetch_test_ticket_fields_and_text(test_key, auth):
    """
    Pull labels/components/fixVersions/versions and custom fields by name,
    plus the Test ticket's summary, description, and text fields (Expected/Actual/Impact).
    """
    # resolve custom ids by name
    portfolio_id = get_field_id_by_name(CUST_TECH_PORTFOLIO_NAME, auth)
    product_id   = get_field_id_by_name(CUST_TECH_PRODUCT_NAME, auth)
    team_id      = get_field_id_by_name(CUST_TECH_DELIVERY_TEAM_NAME, auth)
    expected_id  = get_field_id_by_name(EXPECTED_RESULTS_NAME, auth)
    actual_id    = get_field_id_by_name(ACTUAL_RESULTS_NAME, auth)
    impact_id    = get_field_id_by_name(IMPACT_NAME, auth)

    standard = ["summary", "labels", "components", "fixVersions", "versions", "description", "issuelinks"]
    custom_ids = [fid for fid in [portfolio_id, product_id, team_id, expected_id, actual_id, impact_id] if fid]
    fields_param = ",".join(standard + custom_ids)

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{test_key}"
    r = requests.get(
        url,
        params={"fields": fields_param},
        headers={"Accept": "application/json"},
        auth=auth,
        timeout=30
    )
    r.raise_for_status()
    f = r.json().get("fields", {})

    # Normalize/shape values
    result = {
        "summary": f.get("summary") or "",
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
        },
        "linked_story_key": None
    }

    # description may be string or ADF doc
    desc = f.get("description")
    if isinstance(desc, dict) and desc.get("type") == "doc":
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
        elif val:
            result["custom"][field_id] = val

    # custom single-selects (Portfolio, Product, Delivery Team)
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

    # Capture linked Story key (best-effort via issuelinks)
    for link in (f.get("issuelinks") or []):
        for side in ("outwardIssue", "inwardIssue"):
            other = link.get(side)
            if other and (other.get("fields", {}).get("issuetype", {}).get("name", "").lower() in ("story", "user story")):
                result["linked_story_key"] = other.get("key")
                break
        if result["linked_story_key"]:
            break

    return result

# ============================================================
# ADF BUILDERS (sections as requested)
# ============================================================
def adf_text(text, marks=None):
    node = {"type": "text", "text": text}
    if marks: node["marks"] = marks
    return node

def adf_paragraph(text):
    return {"type": "paragraph", "content": [adf_text(text)]}

def adf_heading(text, level=2):
    return {"type": "heading", "attrs": {"level": level}, "content": [adf_text(text)]}

def adf_bullet_list(items):
    if not items: return []
    return [{
        "type": "bulletList",
        "content": [{"type":"listItem","content":[adf_paragraph(i)]} for i in items]
    }]

def adf_ordered_list(items):
    if not items: return []
    return [{
        "type": "orderedList",
        "content": [{"type":"listItem","content":[adf_paragraph(i)]} for i in items]
    }]

# --- ADF helpers (inline strong) ---
def adf_strong_text(text: str):
    return {"type": "text", "text": text, "marks": [{"type": "strong"}]}

def adf_paragraph_segments(segments):
    """
    segments: list of tuples -> (text_str, is_bold_bool)
    """
    content = []
    for txt, bold in segments:
        if not txt:
            continue
        content.append(adf_strong_text(txt) if bold else adf_text(txt))
    return {"type": "paragraph", "content": content}

def adf_paragraph_with_bold_quotes(line: str):
    """Render a line; anything inside single quotes '...' is bold."""
    segments = []
    parts = re.split(r"(')", line)  # split keeping the quote token
    in_quote = False
    buf = ""
    for p in parts:
        if p == "'":
            if in_quote:
                segments.append((buf, True))
                buf = ""
                in_quote = False
            else:
                if buf:
                    segments.append((buf, False))
                    buf = ""
                in_quote = True
        else:
            buf += p
    if buf:
        segments.append((buf, False))
    return adf_paragraph_segments(segments)

# ============================================================
# AI-LITE HELPERS — Clean, business-focused style
# ============================================================
def normalize_step(s):
    s = s.strip()
    s = re.sub(r'^\s*(?:step\s*\d+[\):\-\]\s]*)', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    return s

def ai_lite_draft(context):
    """
    Produce cleaner, business‑focused content:
    - Summary: from failed step + phase
    - Issue Description: concise impact statement
    - Steps: keep, but mark failed
    - Expected: generic acceptance of selection
    - Actual: concise failure + evidence note
    """
    raw_steps = context.get("zephyr_steps") or []
    zephyr_expected = context.get("zephyr_expected") or []
    steps = [normalize_step(x) for x in raw_steps if x and x.strip()]
    failed_n = int(context.get("failed_step_num") or 0)
    idx = failed_n - 1 if failed_n >= 1 else None
    selected_step = steps[idx] if (idx is not None and idx < len(steps)) else None
    phase = (context.get("test_phase") or "").strip()

    # Extract option + field (from step text "Select 'No' from 'Field' dropdown")
    option, field = None, None
    if selected_step:
        m = re.search(r"Select\s+'([^']+)'\s+from\s+'([^']+)'", selected_step, flags=re.I)
        if m:
            option, field = m.group(1), m.group(2)

    # Expected Results
    expected_txt = (context.get("test_expected_results") or "").strip()
    if not expected_txt:
        expected_txt = "System should accept the selection and continue the workflow without errors."

    # Actual Results
    actual_txt = (context.get("test_actual_results") or "").strip()
    if not actual_txt:
        if option:
            actual_txt = f"System fails to proceed after selecting '{option}', blocking the process. See attachments for details."
        else:
            actual_txt = "System fails to proceed, blocking the process. See attachments for details."

    # Issue Description (business context first)
    if option and field:
        phase_prefix = f"During {phase} testing, " if phase else ""
        issue_desc = f"{phase_prefix}selecting '{option}' in '{field}' fails to proceed as expected, preventing completion of the workflow."
    else:
        phase_prefix = f"During {phase} testing, " if phase else ""
        issue_desc = f"{phase_prefix}the workflow fails to proceed due to a selection error."

    # Mark failed step for visibility
    if idx is not None and idx < len(steps):
        steps[idx] = f"[FAILED] {steps[idx]}"

    # Summary/title suggestion
    if option and field:
        summary = f"Incorrect behavior when selecting '{option}' in '{field}'" + (f" during {phase}" if phase else "")
    else:
        summary = f"Workflow does not proceed after selection" + (f" during {phase}" if phase else "")

    return {
        "summary_suggestion": summary,
        "issue_description": issue_desc,
        "steps_to_reproduce": steps,
        "expected_results": expected_txt,
        "actual_results": actual_txt,
        "confidence": "n/a"
    }

def make_adf_from_ai(test_key, ai, evidence_names):
    content = []
    # Issue Description
    content.append(adf_heading("Issue Description"))
    desc_txt = (ai.get("issue_description") or f"Related Test Ticket: {test_key}").strip()
    content.append(adf_paragraph(desc_txt))

    # Steps to reproduce (bold quoted phrases)
    content.append(adf_heading("Steps to reproduce"))
    steps = ai.get("steps_to_reproduce") or []
    if steps:
        content.append({"type": "orderedList", "content": [
            {"type": "listItem", "content": [adf_paragraph_with_bold_quotes(s)]} for s in steps
        ]})
    else:
        content.append(adf_paragraph("(not provided)"))

    # Expected Results
    content.append(adf_heading("Expected Results"))
    expected = (ai.get("expected_results") or "").strip()
    content.append(adf_paragraph(expected if expected else "(not provided)"))

    # Actual results
    content.append(adf_heading("Actual results"))
    actual = (ai.get("actual_results") or "").strip()
    content.append(adf_paragraph(actual if actual else "(not provided)"))

    # Evidences
    content.append(adf_heading("Evidences"))
    content.extend(adf_bullet_list(evidence_names) if evidence_names else [adf_paragraph("See attachments.")])

    return {"type": "doc", "version": 1, "content": content}

# ============================================================
# ZEPHYR: STEPS/EXPECTED, EXECUTION SEARCH, STEP FAIL, LINK DEFECT, FAIL EXECUTION
# ============================================================
def get_zephyr_steps_and_expected(jira_test_key, auth):
    issue_id = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)
    rel = f"/public/rest/api/1.0/teststep/{issue_id}"
    rows = zephyr_get(rel, query_params={"projectId": project_id})
    steps, expected = [], []
    for s in sorted(rows, key=lambda x: x.get("orderId", 0)):
        step_txt = (s.get("step") or "").strip()
        exp_txt  = (s.get("result") or "").strip()
        if step_txt:
            steps.append(step_txt)
        if exp_txt:
            expected.append(exp_txt)
    return steps, expected

def find_latest_execution_id(jira_test_key, auth):
    issue_id = jira_issue_id_from_key(jira_test_key, auth)
    project_id = jira_project_id_from_key(PROJECT_KEY, auth)
    try:
        rel = "/public/rest/api/1.0/executions"
        params = {"issueId": issue_id, "projectId": project_id, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        # st.write("DEBUG: Executions API response:", data)  # optional
        execs = data.get("executions") or []
        if execs:
            exec_ids = [e.get("execution", {}).get("id") for e in execs if e.get("execution")]
            if exec_ids:
                return exec_ids[0]  # latest
    except Exception as e:
        st.warning(f"Executions API failed: {e}")
    # Fallback: ZQL search
    try:
        rel = "/public/rest/api/1.0/zql/executeSearch"
        zql = f'issue = "{jira_test_key}" ORDER BY executionDate DESC'
        params = {"zqlQuery": zql, "maxRecords": 50, "offset": 0}
        data = zephyr_get(rel, query_params=params)
        # st.write("DEBUG: ZQL API response:", data)  # optional
        execs = (data.get("searchResult") or {}).get("executions") or []
        if execs:
            exec_ids = [e.get("execution", {}).get("id") for e in execs if e.get("execution")]
            if exec_ids:
                return exec_ids[0]
    except Exception as e:
        st.warning(f"ZQL API failed: {e}")
    return None

def link_defect_to_execution(execution_id, defect_issue_key, auth):
    defect_issue_id = jira_issue_id_from_key(defect_issue_key, auth)
    rel = f"/public/rest/api/1.0/execution/{execution_id}/defects"
    body = {"issueId": defect_issue_id}
    return zephyr_post(rel, json_body=body)

def fail_zephyr_step(execution_id, failed_step_num):
    # Fetch step results for execution (if supported)
    rel_steps = f"/public/rest/api/1.0/execution/{execution_id}/steps"
    steps = zephyr_get(rel_steps)
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("No step results returned for this execution.")
    steps_sorted = sorted(steps, key=lambda x: x.get("orderId", 0))
    idx = max(1, int(failed_step_num)) - 1
    if idx >= len(steps_sorted):
        idx = len(steps_sorted) - 1
    step_result_id = steps_sorted[idx].get("id") or steps_sorted[idx].get("stepResultId")
    if not step_result_id:
        raise RuntimeError("Couldn't resolve stepResultId from Zephyr response.")
    rel_update = f"/public/rest/api/1.0/execution/{execution_id}/stepResult/{step_result_id}"
    body = {"status": {"id": 2}}  # 2 = Fail
    zephyr_put(rel_update, json_body=body)

def fail_execution(execution_id):
    rel = f"/public/rest/api/1.0/execution/{execution_id}"
    body = {"status": {"id": 2}}  # 2 = Fail
    zephyr_put(rel, json_body=body)

# ============================================================
# EPIC LINKING (Parent/Epic)
# ============================================================
def try_set_defect_parent_to_epic(defect_key, test_fetch, auth):
    story_key = test_fetch.get("linked_story_key")
    if not story_key:
        return False, "No linked Story found on Test ticket."
    # Fetch Story to discover Epic
    url_story = f"{JIRA_BASE_URL}/rest/api/3/issue/{story_key}"
    r = requests.get(url_story, params={"fields": "parent"}, headers={"Accept":"application/json"}, auth=auth, timeout=30)
    r.raise_for_status()
    fields = r.json().get("fields", {})
    epic_key = None
    if fields.get("parent", {}).get("key"):
        epic_key = fields["parent"]["key"]
    else:
        # fallback: use 'Epic Link' custom field if available
        epic_link_fid = get_field_id_by_name("Epic Link", auth)
        if epic_link_fid:
            r2 = requests.get(url_story, params={"fields": epic_link_fid}, headers={"Accept":"application/json"}, auth=auth, timeout=30)
            r2.raise_for_status()
            el = r2.json().get("fields", {}).get(epic_link_fid)
            if isinstance(el, dict) and el.get("key"):
                epic_key = el.get("key")
            elif isinstance(el, str) and el:
                epic_key = el
    if not epic_key:
        return False, "No Epic found for linked Story."
    # First attempt: set 'parent' to Epic
    try:
        r3 = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{defect_key}",
            json={"fields": {"parent": {"key": epic_key}}},
            headers=headers_json(),
            auth=auth,
            timeout=30
        )
        if r3.status_code in (200, 204):
            return True, f"Parent set to Epic {epic_key}."
    except Exception:
        pass
    # Fallback: try 'Epic Link' field edit
    epic_link_fid = get_field_id_by_name("Epic Link", auth)
    if epic_link_fid:
        r4 = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{defect_key}",
            json={"fields": {epic_link_fid: epic_key}},
            headers=headers_json(),
            auth=auth,
            timeout=30
        )
        if r4.status_code in (200, 204):
            return True, f"Epic Link set to {epic_key}."
        return False, f"Failed to set Epic Link: {r4.status_code} {r4.text[:300]}"
    return False, "Epic Link field not available on this project."

# ============================================================
# STREAMLIT UI
# ============================================================
st.set_page_config(page_title="Jira Defect Creator", layout="centered")

# --- NEW: Title bar (logo + title), green divider, fixed footer ---
add_titlebar_branding(
    header_image_path="mg_branding.png",  # change to "assets/mg_branding.png" if moved
    app_title="🐞 AutoDefect Logger",
    app_subtitle="",
    footer_text="AutoDefect Logger • © 2026",
    # brand_green_hex="#00A878",  # uncomment to force exact brand colors
    # brand_teal_hex="#004D53",
    logo_side="right",
    logo_height_px=48,
    max_inner_width_px=1200
)

# Optional helper note under the bar
st.markdown("**Fields marked with * are mandatory**")

test_ticket      = st.text_input("Test Ticket Number * (e.g. CT-12345)", value="")
failed_step_num  = st.number_input("Failed Test Step Number *", min_value=1, value=1, step=1)
severity         = st.selectbox("Severity *", ["Sev-1", "Sev-2", "Sev-3", "Sev-4"])
priority         = st.selectbox("Priority *", ["Critical", "Major", "Medium", "Minor"])
test_phase       = st.selectbox("Test Phase *", ["FAT", "SIT", "Regression", "Performance", "Production", "NFT", "E2E", "QA"])
uploaded_files   = st.file_uploader("📎 Attach Evidence (screenshots, logs)", accept_multiple_files=True)

# ============================================================
# AI-LITE GENERATION
# ============================================================
use_ai = st.checkbox("Use AI-lite to draft description & steps (beta)", value=True)
if st.button("🧠 Generate Draft (AI-lite)"):
    if not test_ticket.strip():
        st.error("Enter the Test Ticket key first.")
        st.stop()
    auth = get_auth()
    # Background pulls
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip(), auth)
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")
    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip(), auth)
    except Exception as e:
        copied = {
            "summary": "",
            "labels": [], "components": [], "fixVersions": [], "versions": [],
            "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""},
            "linked_story_key": None
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
        "labels": copied.get("labels", []),
        "components": copied.get("components", []),
        "versions": copied.get("versions", []),
        "fixVersions": copied.get("fixVersions", []),
        "evidence_names": evidence_names
    }
    ai_out = ai_lite_draft(ctx)
    st.session_state["ai_out"] = ai_out
    st.session_state["ctx"] = ctx
    st.success("AI-lite draft generated. Review and edit below.")

# Editable preview (Impact & AI notes removed)
if use_ai and "ai_out" in st.session_state:
    ai = st.session_state["ai_out"]
    ai["issue_description"] = st.text_area("Issue Description", ai.get("issue_description", ""), height=120)
    steps_txt = "\n".join(ai.get("steps_to_reproduce", []))
    steps_txt = st.text_area("Steps to reproduce (one per line)", steps_txt, height=150)
    ai["steps_to_reproduce"] = [s.strip() for s in steps_txt.splitlines() if s.strip()]
    ai["expected_results"]   = st.text_area("Expected Results", ai.get("expected_results", ""), height=100)
    ai["actual_results"]     = st.text_area("Actual Results", ai.get("actual_results", ""), height=100)

# ============================================================
# HELPERS: SUMMARY, TRANSITION TO FAILED
# ============================================================
def build_defect_summary_from_test(copied):
    base = (copied.get("summary") or "").strip()
    if base and base.lower() not in ("testing", "test", "defect"):
        return base
    ai = st.session_state.get("ai_out")
    if ai and ai.get("summary_suggestion"):
        return ai["summary_suggestion"]
    return "Observed issue during test execution"

def transition_issue_to_failed(issue_key, auth):
    # Discover transition id named 'Fail' (or 'Fail') dynamically
    r = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/transitions",
        headers={"Accept":"application/json"},
        auth=auth,
        timeout=30
    )
    if r.status_code != 200:
        st.warning(f"Transitions fetch failed: {r.status_code} {r.text[:300]}")
        return False
    transitions = (r.json() or {}).get("transitions", [])
    target = None
    for t in transitions:
        name = (t.get("name") or "").strip().lower()
        to_name = (t.get("to", {}).get("name") or "").strip().lower()
        if name in ("FAIL", "FAILED") or to_name in ("failed", "fail", "fail status", "fail state", "fail"):
            target = t.get("id")
            break
    if not target:
        for t in transitions:
            if str(t.get("id")) == "51":
                target = "51"
                break
    if not target:
        st.warning("Could not locate a 'Failed' transition for this issue.")
        return False
    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/transitions",
        json={"transition": {"id": str(target)}},
        headers=headers_json(),
        auth=auth,
        timeout=30
    )
    if resp.status_code not in (200, 204):
        st.warning(f"Failed to update Test ticket status: {resp.status_code} {resp.text[:300]}")
        return False
    return True

# ============================================================
# CREATE DEFECT
# ============================================================
if st.button("🚀 Create Defect"):
    if not all([test_ticket.strip(), severity, priority, test_phase]):
        st.error("Please fill all mandatory fields (*)")
        st.stop()
    auth = get_auth()

    # Resolve issue type & field meta
    url_it = f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes"
    r_it = requests.get(url_it, headers={"Accept":"application/json"}, auth=auth, timeout=30)
    if r_it.status_code != 200:
        st.error(f"Failed to fetch issue types: {r_it.status_code} {r_it.text[:300]}")
        st.stop()
    it_by_name = {it.get("name"): it.get("id") for it in (r_it.json().get("issueTypes") or []) if it.get("id")}
    issue_type_id = next((it_by_name[n] for n in ISSUE_TYPE_CANDIDATES if n in it_by_name), None) or (r_it.json().get("issueTypes") or [{}])[0].get("id")

    fields_meta = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/createmeta/{PROJECT_KEY}/issuetypes/{issue_type_id}",
        headers={"Accept":"application/json"},
        auth=auth,
        timeout=30
    ).json()

    def get_option_id(fields_meta_obj, field_id, chosen_label):
        for f in fields_meta_obj.get("fields", []):
            if f.get("fieldId") == field_id:
                for opt in (f.get("allowedValues") or []):
                    label = opt.get("value") or opt.get("name")
                    if label == chosen_label:
                        return opt.get("id")
        return None

    severity_id   = get_option_id(fields_meta, SEVERITY_FIELD_ID, severity)
    test_phase_id = get_option_id(fields_meta, TEST_PHASE_FIELD_ID, test_phase)
    if not severity_id or not test_phase_id:
        st.warning("Severity or Test Phase option not found on create screen; will try label fallback.")

    # Background pulls
    try:
        steps_list, expected_from_zephyr = get_zephyr_steps_and_expected(test_ticket.strip(), auth)
    except Exception as e:
        steps_list, expected_from_zephyr = [], []
        st.warning(f"Zephyr fetch failed: {e}")
    try:
        copied = fetch_test_ticket_fields_and_text(test_ticket.strip(), auth)
    except Exception as e:
        copied = {
            "summary": "",
            "labels": [], "components": [], "fixVersions": [], "versions": [],
            "custom": {}, "text": {"issue_description": "", "expected_results": "", "actual_results": "", "impact": ""},
            "linked_story_key": None
        }
        st.warning(f"Could not fetch fields/description from Test: {e}")

    evidence_names = [f.name for f in (uploaded_files or [])]

    # Ensure AI‑lite context exists for clean style
    if use_ai and "ai_out" not in st.session_state:
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
            "labels": copied.get("labels", []),
            "components": copied.get("components", []),
            "versions": copied.get("versions", []),
            "fixVersions": copied.get("fixVersions", []),
            "evidence_names": evidence_names
        }
        st.session_state["ai_out"] = ai_lite_draft(ctx)

    # Summary/title improved (uses Test summary if meaningful, else AI suggestion)
    summary = build_defect_summary_from_test(copied)

    # Build description (ADF — Jira Cloud requires ADF)
    if use_ai and "ai_out" in st.session_state:
        description_adf = make_adf_from_ai(
            test_key=test_ticket.strip(),
            ai=st.session_state["ai_out"],
            evidence_names=evidence_names
        )
    else:
        description_adf = {
            "type": "doc", "version": 1,
            "content": [
                adf_heading("Issue Description"),
                adf_paragraph(copied["text"]["issue_description"] or f"Related Test Ticket: {test_ticket.strip()}")
            ]
        }

    # Create payload — ensure description is ADF; use option ids if available, else label values
    create_fields = {
        "project": {"key": PROJECT_KEY},
        "issuetype": {"id": issue_type_id},
        "summary": summary,
        "description": description_adf,
        "priority": {"name": priority}
    }
    if severity_id:
        create_fields[SEVERITY_FIELD_ID] = {"id": severity_id}
    else:
        create_fields[SEVERITY_FIELD_ID] = {"value": severity}
    if test_phase_id:
        create_fields[TEST_PHASE_FIELD_ID] = {"id": test_phase_id}
    else:
        create_fields[TEST_PHASE_FIELD_ID] = {"value": test_phase}

    create_payload = {"fields": create_fields}

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
    st.session_state["issue_key"] = issue_key  # Persist for reruns
    st.success(f"✅ Defect created: {issue_key}")

    # -- Update copied fields on the new defect --
    edit_fields = {}
    if copied["labels"]:
        edit_fields["labels"] = [lbl for lbl in copied["labels"] if lbl != "JiraTestGenAI"]
    if copied["components"]:  edit_fields["components"]  = copied["components"]
    if copied["fixVersions"]: edit_fields["fixVersions"] = copied["fixVersions"]
    if copied["versions"]:    edit_fields["versions"]    = copied["versions"]
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

    # Parent/Epic linking (best‑effort)
    ok, msg = try_set_defect_parent_to_epic(issue_key, copied, auth)
    st.info(f"Epic link: {msg}")

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

    # --- Upload attachments (if the user added any)
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

    # Transition Test ticket to Failed (dynamic lookup; falls back to id '51')
    transitioned = transition_issue_to_failed(test_ticket.strip(), auth)
    if transitioned:
        st.success("✅ Test ticket transitioned to 'Failed'.")

    # Zephyr: link defect to latest execution, fail the failed step & execution
    try:
        execution_id = find_latest_execution_id(test_ticket.strip(), auth)
        if not execution_id:
            st.warning("No Zephyr execution found for this Test.")
        else:
            try:
                link_defect_to_execution(execution_id, issue_key, auth)
                st.success("🔗 Defect linked to Zephyr execution (Defects section).")
            except Exception as e:
                st.warning(f"Failed to link defect to Zephyr execution: {e}")
            # Update failed step (best‑effort; may be unsupported on some tenants)
            try:
                if ENABLE_STEP_UPDATE:
                    fail_zephyr_step(execution_id, failed_step_num)
                    st.success("❗ Failed step updated in Zephyr.")
                else:
                    st.info("Step update skipped (ENABLE_STEP_UPDATE = False).")
            except Exception as e:
                st.warning(f"Could not update Zephyr failed step: {e}")
            # Fail the overall execution
            try:
                fail_execution(execution_id)
                st.success("🟥 Zephyr execution status set to FAIL.")
            except Exception as e:
                st.warning(f"Failed to update Zephyr execution status: {e}")
    except Exception as e:
        st.warning(f"Zephyr operations failed: {e}")

    # Safe link rendering (works across reruns)
    ik = st.session_state.get("issue_key")
    if ik:
        st.link_button("Open Defect in Jira", f"{JIRA_BASE_URL}/browse/{ik}")
    else:
        st.info("Create a defect first to enable the Jira link.")
