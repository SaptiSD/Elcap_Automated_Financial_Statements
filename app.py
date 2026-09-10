"""ElCap Scorecard Generator - web front end.

Upload an annual report (or just the pages that carry the statements), pick
the pages, and download a filled ElCap scorecard workbook.

    streamlit run app.py

The extraction, the metric arithmetic and the workbook writing are the same
code the command-line tool runs; this module is the interface around it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from extract_pdf_text import (format_page_selection,  # noqa: E402
                              page_count, parse_page_selection,
                              suggest_statement_pages)
from fill_excel import AUDIT_LINE_ITEMS, BASIS_LABELS  # noqa: E402
from llm_extract import (API_BACKEND, API_MODELS,  # noqa: E402
                         DEFAULT_API_MODEL, api_key_available,
                         available_backends)
from scorecard_pipeline import PipelineError, build_scorecard  # noqa: E402

SAMPLE_PDF = os.path.join(HERE, "sample", "input", "Audited Financial Statements.pdf")
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "elcap_uploads")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: How each metric reads to a human. The workbook always receives the raw
#: ratio; this only governs the screen.
METRIC_DISPLAY: dict[str, tuple[str, str]] = {
    "Revenue": ("currency", "12-month total revenue"),
    "EBIT Margin": ("percent", "EBIT / revenue"),
    "DSCR": ("times", "(Net income + depreciation + interest) / debt service"),
    "EBITDA / Interest Expense": ("times", "EBITDA / interest expense"),
    "Current Ratio": ("times", "Current assets / current liabilities"),
    "OCF Ratio": ("times", "Operating cash flow / current liabilities"),
    "Funded Debt / EBITDA": ("times", "Borrowed funds / EBITDA"),
    "Debt / TNW": ("times", "Total liabilities / tangible net worth"),
    "OCF / Debt": ("percent", "Operating cash flow / total liabilities"),
}

STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Mono:wght@500&display=swap');

:root {
  --ink:      #12243A;
  --muted:    #5C6B7F;
  --line:     #E2E7EE;
  --brand:    #14304F;
  --accent:   #B8860B;
  --good:     #14713D;
  --warn:     #A65C00;
  --surface:  #FFFFFF;
}
html, body, [class*="css"], .stMarkdown, .stApp { font-family: 'Inter', system-ui, sans-serif; }
.block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1180px; }
#MainMenu, footer { visibility: hidden; }

.hero {
  background: linear-gradient(115deg, #14304F 0%, #1E4B72 55%, #2B6E8C 100%);
  color: #fff; border-radius: 16px; padding: 30px 34px; margin-bottom: 26px;
  box-shadow: 0 10px 28px rgba(20,48,79,.18);
}
.hero h1 { font-size: 1.95rem; font-weight: 700; margin: 0 0 6px; letter-spacing: -.02em; }
.hero p  { margin: 0; opacity: .88; font-size: 1.02rem; max-width: 62ch; line-height: 1.5; }
.hero .chips { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
.hero .chip {
  background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.22);
  border-radius: 999px; padding: 4px 12px; font-size: .78rem; font-weight: 500;
}

.step { display: flex; align-items: center; gap: 10px; margin: 26px 0 10px; }
.step .num {
  width: 26px; height: 26px; border-radius: 50%; background: var(--brand); color: #fff;
  display: grid; place-items: center; font-size: .8rem; font-weight: 700; flex: 0 0 26px;
}
.step .txt { font-size: 1.06rem; font-weight: 600; color: var(--ink); }
.step .hint { font-size: .86rem; color: var(--muted); font-weight: 400; }

.facts {
  display: flex; flex-wrap: wrap; gap: 26px; background: var(--surface);
  border: 1px solid var(--line); border-left: 4px solid var(--brand);
  border-radius: 12px; padding: 16px 22px; margin-bottom: 18px;
}
.facts .k { font-size: .72rem; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); }
.facts .v { font-size: 1rem; font-weight: 600; color: var(--ink); margin-top: 2px; }

.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
@media (max-width: 900px) { .grid { grid-template-columns: repeat(2, 1fr); } }
.card {
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  padding: 15px 17px; box-shadow: 0 1px 2px rgba(18,36,58,.05);
}
.card .name { font-size: .78rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.card .val  { font-family: 'Roboto Mono', monospace; font-size: 1.55rem; font-weight: 500; color: var(--ink); margin: 6px 0 3px; }
.card .sub  { font-size: .78rem; color: var(--muted); line-height: 1.35; }
.card.blank { background: #FFFBF2; border-color: #F0DCB4; }
.card.blank .val { color: var(--warn); font-size: 1.15rem; }

.pagerow {
  display: flex; justify-content: space-between; gap: 12px; padding: 7px 12px;
  border: 1px solid var(--line); border-radius: 8px; margin-bottom: 6px; background: var(--surface);
}
.pagerow .p { font-weight: 600; color: var(--brand); font-family: 'Roboto Mono', monospace; }
.pagerow .w { color: var(--muted); font-size: .88rem; }

.note { font-size: .86rem; color: var(--muted); line-height: 1.5; }
div[data-testid="stSidebar"] { background: #F4F6F9; border-right: 1px solid var(--line); }
div[data-testid="stSidebar"] h2 { font-size: 1rem; }
.stDownloadButton button { width: 100%; font-weight: 600; }
</style>
"""


# --------------------------------------------------------------------------
# Configuration and credentials
# --------------------------------------------------------------------------

def load_api_key() -> None:
    """Move an API key from Streamlit secrets or the session into the env.

    The extraction code reads ``ANTHROPIC_API_KEY`` from the environment, so
    a key configured in the cloud's secrets store, or pasted into the sidebar
    for one session, arrives the same way.
    """
    if st.session_state.get("pasted_key"):
        os.environ["ANTHROPIC_API_KEY"] = st.session_state["pasted_key"].strip()
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:                       # no secrets file configured
        key = ""
    if key:
        os.environ["ANTHROPIC_API_KEY"] = str(key)


def sidebar() -> dict:
    """Draw the sidebar and return the run settings it collects."""
    with st.sidebar:
        st.markdown("## Reading engine")
        backends = available_backends()

        if api_key_available():
            st.success("Anthropic API key detected", icon=":material/check_circle:")
        elif backends:
            st.info(f"Using the local **{backends[0]}** CLI. Add an API key to "
                    "run this anywhere.", icon=":material/terminal:")
        else:
            st.error("No reading engine available. Paste an Anthropic API key "
                     "below, or set one in the app's secrets.",
                     icon=":material/error:")

        st.text_input(
            "Anthropic API key", key="pasted_key", type="password",
            placeholder="sk-ant-...",
            help="Held for this browser session only - never written to disk. "
                 "On Streamlit Cloud, set ANTHROPIC_API_KEY in app secrets "
                 "instead and leave this blank.")

        model = st.selectbox(
            "Model", API_MODELS,
            index=API_MODELS.index(DEFAULT_API_MODEL),
            help="Only applies to the Anthropic API engine.",
            disabled=not api_key_available())

        verify = st.toggle(
            "Read twice and compare", value=False,
            help="Reads the statement a second time and lists every line item "
                 "the two passes disagree on. Roughly doubles the run time.")

        st.markdown("## Scorecard template")
        template = st.file_uploader(
            "Use a different template", type=["xlsx"],
            help="Defaults to the bundled ElCap scorecard template. Any "
                 "workbook with a 'Scorecard to ElCap' sheet works.")
        template_path = _stage_template(template) if template else None
        st.caption(
            "Filled: **D5:L5** on *Scorecard to ElCap*. Every rating formula "
            "in the template is left untouched, so the ratings calculate when "
            "the file opens." if not template_path
            else f"Using **{template.name}**.")

        st.markdown("## How it reads")
        st.caption(
            "The model only transcribes line items and says where it found "
            "each one. All nine metrics are arithmetic done in Python, so no "
            "rating depends on the model's own maths.")

        using_api = api_key_available()
        return {
            # A CLI backend has its own idea of which models exist, and
            # handing it an API model id makes it fail on the server side,
            # so the picker only reaches the API path.
            "model": model if using_api else None,
            "verify": verify,
            "template_path": template_path,
            "cli": API_BACKEND if using_api else "auto",
            "ready": using_api or bool(backends),
        }


def _stage_template(upload) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"template_{_digest(upload.getvalue())}.xlsx")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(upload.getvalue())
    return path


# --------------------------------------------------------------------------
# Choosing a document
# --------------------------------------------------------------------------

def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:16]


def stage_upload(upload) -> str:
    """Write an uploaded file to a temp path and return it.

    The content digest names the *folder*, not the file, so two uploads never
    collide and the workbook still inherits the name the user recognises.
    """
    folder = os.path.join(UPLOAD_DIR, _digest(upload.getvalue()))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _safe(upload.name))
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(upload.getvalue())
    return path


def _safe(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)
    return keep[:80]


@st.cache_data(show_spinner=False)
def scan_pages(path: str, signature: str) -> dict:
    """Classify a PDF's pages once per file rather than on every rerun.

    ``signature`` is part of the cache key, not the work: a file replaced at
    the same path must be re-indexed rather than answered from the cache.
    """
    return suggest_statement_pages(path)


def document_picker() -> str | None:
    """Let the user choose a document; return its path on disk."""
    upload_tab, edgar_tab, sample_tab = st.tabs(
        ["Upload a file", "Fetch from SEC EDGAR", "Use the sample"])

    with upload_tab:
        upload = st.file_uploader(
            "Annual report, 10-K, or just the statement pages",
            type=["pdf", "htm", "html", "txt"], key="statement_upload",
            help="PDF, an SEC HTML exhibit, or plain text. Up to 200 MB.")
        st.markdown(
            '<p class="note">The whole annual report is fine - you pick the '
            'pages next. Four to ten pages of tables (balance sheet, income '
            'statement, cash flows, and the debt and goodwill notes) is what '
            'the scorecard needs.</p>', unsafe_allow_html=True)
        if upload is not None:
            return stage_upload(upload)

    with edgar_tab:
        st.markdown(
            '<p class="note">Pulls the most recent statements filed with the '
            'SEC for a ticker - an audited-statement exhibit where one exists, '
            'otherwise a statement built from the company\'s XBRL facts.</p>',
            unsafe_allow_html=True)
        left, right = st.columns([2, 1], vertical_alignment="bottom")
        ticker = left.text_input("Ticker symbol", placeholder="TSLA",
                                 label_visibility="collapsed").strip().upper()
        if right.button("Fetch filing", width="stretch", disabled=not ticker):
            path = fetch_from_edgar(ticker)
            if path:
                st.session_state["edgar_path"] = path
        if st.session_state.get("edgar_path"):
            path = st.session_state["edgar_path"]
            st.success(f"Fetched **{os.path.basename(path)}**",
                       icon=":material/download_done:")
            return path

    with sample_tab:
        st.markdown(
            '<p class="note">Zencoder Inc. FY2011 - a 30-page set of audited '
            'statements, bundled with the project. Good for a first run when '
            'you have no filing to hand.</p>', unsafe_allow_html=True)
        if st.button("Load the sample statements", width="stretch"):
            st.session_state["use_sample"] = True
        if st.session_state.get("use_sample") and os.path.exists(SAMPLE_PDF):
            return SAMPLE_PDF

    return None


def fetch_from_edgar(ticker: str) -> str | None:
    from edgar_fetch import EDGARError, fetch_financial_statement

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with st.spinner(f"Searching EDGAR for {ticker}..."):
        try:
            path, _kind = fetch_financial_statement(ticker, UPLOAD_DIR)
            return path
        except EDGARError as exc:
            st.error(str(exc), icon=":material/error:")
        except Exception as exc:
            st.error(f"EDGAR fetch failed: {exc}", icon=":material/error:")
    return None


# --------------------------------------------------------------------------
# Choosing pages
# --------------------------------------------------------------------------

def page_picker(path: str) -> list[int] | None:
    """Offer the detected statement pages and return the user's selection.

    Returns None for a document with no pages to choose from (HTML, text),
    which the pipeline reads whole.
    """
    if not path.lower().endswith(".pdf"):
        st.info("Text and HTML filings are read in full - no page selection "
                "needed.", icon=":material/description:")
        return None

    try:
        total = page_count(path)
        found = scan_pages(path, signature=f"{os.path.getmtime(path)}")
    except Exception as exc:
        st.warning(f"Could not index the pages ({exc}); the whole document "
                   "will be read.", icon=":material/warning:")
        return None

    suggestion = found["pages"]
    default = format_page_selection(suggestion) or f"1-{min(total, 12)}"

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        if suggestion:
            st.markdown(f"**Found statements on {len(suggestion)} of "
                        f"{total} pages**")
            for number in suggestion:
                st.markdown(
                    f'<div class="pagerow"><span class="p">p.{number}</span>'
                    f'<span class="w">{found["labels"][number]}</span></div>',
                    unsafe_allow_html=True)
        else:
            st.warning(
                f"No statement headings were recognised in these {total} "
                "pages. Enter the pages yourself - the balance sheet and "
                "income statement are what matter most.",
                icon=":material/search_off:")

    with right:
        selection = st.text_input(
            "Pages to read", value=default,
            help="Ranges and single pages, e.g. 47-50, 63, 71.")
        try:
            pages = parse_page_selection(selection, total)
        except ValueError as exc:
            st.error(str(exc), icon=":material/error:")
            return []
        if not pages:
            st.error("Select at least one page.", icon=":material/error:")
            return []
        st.markdown(
            f'<p class="note">Reading <b>{len(pages)}</b> of {total} pages. '
            'Everything outside this range is ignored, which is why a 200-page '
            'report costs the same as a four-page extract.</p>',
            unsafe_allow_html=True)
        return pages


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

def format_metric(name: str, value) -> str:
    if value is None:
        return "not computable"
    kind = METRIC_DISPLAY.get(name, ("times", ""))[0]
    if kind == "currency":
        return f"${value:,.0f}"
    if kind == "percent":
        return f"{value * 100:,.1f}%"
    return f"{value:,.2f}x"


def render_result(result) -> None:
    fin = result.financials

    st.markdown('<div class="step"><div class="num">4</div>'
                '<div><div class="txt">Scorecard</div></div></div>',
                unsafe_allow_html=True)

    facts = [
        ("Company", fin.company_name or "not identified"),
        ("Fiscal year", fin.fiscal_year or "not identified"),
        ("Figures printed in", fin.unit_scale),
        ("Statements", BASIS_LABELS.get(fin.statement_basis, fin.statement_basis)),
        ("Pages read", format_page_selection(result.pages) if result.pages else "whole file"),
        ("Run time", f"{result.seconds:.0f}s"),
    ]
    st.markdown(
        '<div class="facts">' + "".join(
            f'<div><div class="k">{k}</div><div class="v">{v}</div></div>'
            for k, v in facts) + "</div>", unsafe_allow_html=True)

    st.download_button(
        f"Download {result.workbook_name}", data=result.workbook_bytes,
        file_name=result.workbook_name, mime=XLSX_MIME, type="primary",
        icon=":material/download:", width="stretch")
    st.markdown(
        '<p class="note">Open it in Excel and the ElCap ratings calculate '
        'themselves. The <b>Extraction Audit</b> sheet inside carries every '
        'figure, the line it was read from, and these warnings.</p>',
        unsafe_allow_html=True)

    cards = []
    for metric, value in result.metric_pairs:
        blank = value is None
        detail = metric.detail if blank else METRIC_DISPLAY.get(
            metric.name, ("", ""))[1]
        cards.append(
            f'<div class="card{" blank" if blank else ""}">'
            f'<div class="name">{metric.name}</div>'
            f'<div class="val">{format_metric(metric.name, value)}</div>'
            f'<div class="sub">{detail or "&nbsp;"}</div></div>')
    st.markdown('<div class="grid">' + "".join(cards) + "</div>",
                unsafe_allow_html=True)

    if result.blank_metrics:
        st.warning(
            f"{len(result.blank_metrics)} of 9 metrics could not be computed: "
            f"{', '.join(result.blank_metrics)}. The template still prints a "
            "rating for a blank cell, so those ratings are not meaningful.",
            icon=":material/report:")

    audit, warnings, source, raw = st.tabs(
        [f"Extraction audit ({_read_count(fin)} line items)",
         f"Warnings ({len(result.warnings) + len(result.disagreements)})",
         "Statement text", "JSON"])

    with audit:
        st.dataframe(audit_rows(fin), width="stretch", hide_index=True,
                     column_config={
                         "As printed": st.column_config.NumberColumn(
                             format="localized"),
                         "In dollars": st.column_config.NumberColumn(
                             format="localized",
                             help="After the statement's units are applied."),
                         "Source in the statement": st.column_config.TextColumn(
                             width="large")})

    with warnings:
        if result.disagreements:
            st.markdown("**The two passes disagreed on:**")
            for line in result.disagreements:
                st.error(line, icon=":material/compare_arrows:")
        for line in result.warnings:
            st.warning(line, icon=":material/warning:")
        for line in result.notes:
            st.info(line, icon=":material/sticky_note_2:")
        if not (result.warnings or result.disagreements or result.notes):
            st.success("Nothing flagged.", icon=":material/check_circle:")

    with source:
        st.caption(f"{len(result.statement_text):,} characters were sent to the "
                   "model - exactly what is shown here.")
        st.text_area("Statement text", result.statement_text, height=380,
                     label_visibility="collapsed")

    with raw:
        st.json(result.to_json_dict(), expanded=False)
        st.download_button(
            "Download the extraction as JSON",
            data=json.dumps(result.to_json_dict(), indent=2),
            file_name=os.path.splitext(result.workbook_name)[0] + ".json",
            mime="application/json", icon=":material/data_object:")


def _read_count(fin) -> int:
    return sum(1 for field, _ in AUDIT_LINE_ITEMS
               if getattr(fin, field, None) is not None)


def audit_rows(fin) -> list[dict]:
    rows = []
    for field, label in AUDIT_LINE_ITEMS:
        dollars = getattr(fin, field, None)
        if field in fin.absent_line_items:
            status = "not on the statement (treated as 0)"
        elif dollars is None:
            status = "NOT FOUND - metric left blank"
        else:
            status = "read from the statement"
        rows.append({
            "Line item": label,
            "As printed": fin.as_reported.get(field),
            "In dollars": dollars,
            "Status": status,
            "Source in the statement": fin.sources.get(field, ""),
        })
    return rows


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="ElCap Scorecard Generator",
                       page_icon=":material/finance:", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(STYLE, unsafe_allow_html=True)
    load_api_key()

    st.markdown(
        '<div class="hero"><h1>ElCap Scorecard Generator</h1>'
        '<p>Turn an annual report into a filled ElCap credit scorecard. '
        'Upload the filing, confirm the pages that carry the statements, and '
        'download the workbook with all nine metrics computed and every '
        'figure traced back to the line it came from.</p>'
        '<div class="chips"><span class="chip">PDF, SEC HTML or text</span>'
        '<span class="chip">9 credit metrics</span>'
        '<span class="chip">Audit trail in every workbook</span>'
        '<span class="chip">Template formulas preserved</span></div></div>',
        unsafe_allow_html=True)

    settings = sidebar()

    st.markdown('<div class="step"><div class="num">1</div><div>'
                '<div class="txt">Choose a document</div>'
                '<div class="hint">An annual report, a 10-K, an SEC exhibit, '
                'or just the pages you pulled out of one.</div></div></div>',
                unsafe_allow_html=True)
    path = document_picker()

    if not path:
        st.stop()

    st.markdown('<div class="step"><div class="num">2</div><div>'
                '<div class="txt">Confirm the pages</div>'
                '<div class="hint">Detected below - correct them if a '
                'statement was missed.</div></div></div>',
                unsafe_allow_html=True)
    pages = page_picker(path)

    st.markdown('<div class="step"><div class="num">3</div><div>'
                '<div class="txt">Generate the scorecard</div></div></div>',
                unsafe_allow_html=True)

    blocked = pages == [] or not settings["ready"]
    if not settings["ready"]:
        st.error("Add an Anthropic API key in the sidebar before generating.",
                 icon=":material/key_off:")
    if st.button("Generate the ElCap scorecard", type="primary",
                 icon=":material/table_view:", disabled=blocked,
                 width="stretch"):
        run(path, pages, settings)

    if st.session_state.get("result"):
        render_result(st.session_state["result"])


def run(path: str, pages, settings: dict) -> None:
    st.session_state.pop("result", None)
    failure: tuple[str, str] | None = None

    with st.status("Working...", expanded=True) as status:
        def say(message: str) -> None:
            status.update(label=message)
            st.write(message)

        try:
            result = build_scorecard(
                path, pages=pages, cli=settings["cli"],
                model=settings["model"], verify=settings["verify"],
                template_path=settings["template_path"], progress=say)
        except PipelineError as exc:
            failure = ("Could not read this document", str(exc))
        except Exception as exc:
            failure = ("The run failed", f"{type(exc).__name__}: {exc}")

        if failure:
            status.update(label=failure[0], state="error", expanded=False)
        else:
            status.update(label=f"Scorecard ready in {result.seconds:.0f}s",
                          state="complete", expanded=False)

    # Outside the status box: a collapsed container would hide the one thing
    # the user needs to read when a run fails.
    if failure:
        st.error(failure[1], icon=":material/error:")
        return
    st.session_state["result"] = result


if __name__ == "__main__":
    main()
