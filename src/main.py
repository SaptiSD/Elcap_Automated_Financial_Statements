"""ElCap financial statement -> scorecard Excel generator.

Two ways in:

    python src/main.py                          interactive menu
    python src/main.py statement.pdf            one file, straight through
    python src/main.py input/*.pdf --verify     several files, two-pass check
    python src/main.py --ticker CAT             pull from SEC EDGAR first

The pipeline:
  text extraction -> LLM structured extraction -> deterministic metrics
    -> filled Excel scorecard (+ an Extraction Audit sheet).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Statements carry typographic quotes, dashes and accented company names, and
# a legacy Windows console is cp1252. Without this, printing an extracted note
# raises UnicodeEncodeError and aborts a run whose figures were already read.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable text stream
        pass

ROOT = os.path.dirname(HERE)
INPUT_DIR = os.path.join(ROOT, "input")
DEFAULT_CLI = os.environ.get("ELCAP_LLM_CLI", "auto")
DEFAULT_MODEL = os.environ.get("ELCAP_MODEL") or None

SUPPORTED_EXTENSIONS = (".pdf", ".htm", ".html", ".txt")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Turn a financial statement into a populated ElCap scorecard.",
    )
    parser.add_argument("files", nargs="*",
                        help="statement files (.pdf/.htm/.html/.txt). "
                             "Omit for the interactive menu.")
    parser.add_argument("--ticker", action="append", default=[], metavar="SYM",
                        help="download a statement from SEC EDGAR first "
                             "(repeatable)")
    parser.add_argument("--out-dir", default=None, metavar="DIR",
                        help="where to write workbooks (default: output/)")
    parser.add_argument("--template", default=None, metavar="XLSX",
                        help="scorecard template to fill "
                             "(default: template/ElCap Scorecard Template.xlsx)")
    parser.add_argument("--cli", default=DEFAULT_CLI,
                        choices=["auto", "api", "compat", "opencode", "claude", "codex", "gemini"],
                        help="LLM CLI backend (default: %(default)s)")
    parser.add_argument("--model", default=DEFAULT_MODEL, metavar="ID",
                        help="model id passed to the backend")
    parser.add_argument("--verify", action="store_true",
                        help="read the statement twice and report any line "
                             "items the two passes disagree on")
    parser.add_argument("--json", default=None, metavar="FILE",
                        help="also write the extracted line items as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    sources = _expand_sources(args.files)
    for ticker in args.ticker:
        path = _fetch_ticker(ticker)
        if path:
            sources.append(path)

    if not sources and not args.files and not args.ticker:
        return interactive_main(args)

    if not sources:
        print("[ERROR] No usable statement files.")
        return 2

    failures = 0
    for source in sources:
        try:
            process_file(source, args)
        except Exception as exc:
            failures += 1
            print(f"\n[ERROR] {os.path.basename(source)}: {exc}")
    if len(sources) > 1:
        print(f"\n{len(sources) - failures}/{len(sources)} statements processed.")
    return 1 if failures else 0


def _expand_sources(patterns: list[str]) -> list[str]:
    """Resolve CLI arguments into existing, supported statement files."""
    resolved: list[str] = []
    for pattern in patterns:
        matches = glob.glob(pattern) or ([pattern] if os.path.isfile(pattern) else [])
        if not matches:
            print(f"[WARN] No file matched: {pattern}")
            continue
        for match in sorted(matches):
            if not os.path.isfile(match):
                continue
            if not match.lower().endswith(SUPPORTED_EXTENSIONS):
                print(f"[WARN] Unsupported file type, skipping: {match}")
                continue
            resolved.append(match)
    return resolved


def _fetch_ticker(ticker: str) -> str | None:
    from edgar_fetch import EDGARError, fetch_financial_statement

    try:
        path, _kind = fetch_financial_statement(ticker.strip().upper(), INPUT_DIR)
    except EDGARError as exc:
        print(f"[ERROR] {ticker}: {exc}")
        return None
    except Exception as exc:
        print(f"[ERROR] {ticker}: EDGAR fetch failed: {exc}")
        return None
    print(f"Saved statement to: {path}")
    return path


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

def process_file(source: str, args) -> str:
    """Run one statement end to end and return the workbook path."""
    import json

    from compute_metrics import (blank_metric_warnings, compute_metrics,
                                 render_metrics_for_excel)
    from extract_pdf_text import check_text_layer, extract_statement_text
    from fill_excel import (default_output_dir, generate_output_name,
                            write_metrics_to_template)
    from llm_extract import (extract_financials, extract_financials_verified,
                             resolve_backend)

    print(f"\n--- Processing: {os.path.basename(source)} ---")

    print("[1/4] Extracting text...")
    text = extract_statement_text(source)
    print(f"      Extracted {len(text):,} characters.")

    scan_warning = check_text_layer(source) if source.lower().endswith(".pdf") else None
    if scan_warning:
        # Said before the LLM runs: a scanned statement cannot be read, and the
        # user should reach for OCR rather than wait out three failed attempts.
        print(f"      WARNING: {scan_warning}")

    backend = resolve_backend(args.cli)
    passes = "two passes" if args.verify else "one pass"
    print(f"[2/4] Reading line items via {backend} ({passes}, this can take a minute)...")
    if args.model:
        print(f"      model: {args.model}")
    if args.verify:
        fin, disagreements = extract_financials_verified(
            text, model=args.model, cli=args.cli)
    else:
        fin = extract_financials(text, model=args.model, cli=args.cli)
        disagreements = []
    _print_financials(fin)

    print("[3/4] Computing the 9 credit metrics...")
    metrics = compute_metrics(fin)
    values = render_metrics_for_excel(metrics)
    for m, v in zip(metrics, values):
        detail = f"   {m.detail}" if m.detail else ""
        print(f"      {m.name:26s} {_fmt(v):>18s}{detail}")

    warnings = fin.validate() + blank_metric_warnings(metrics)
    if scan_warning:
        warnings.insert(0, scan_warning)
    for note in fin.notes:
        print(f"      note: {note}")
    for warning in warnings:
        print(f"      warning: {warning}")
    for diff in disagreements:
        print(f"      DISAGREEMENT: {diff}")

    print("[4/4] Writing Excel scorecard...")
    out_dir = args.out_dir or default_output_dir()
    out_path = os.path.join(out_dir, generate_output_name(source))
    write_metrics_to_template(
        values, out_path,
        template_path=args.template,
        financials=fin, metrics=metrics, source_file=source,
        warnings=warnings, disagreements=disagreements,
    )
    print(f"\nDONE -> {out_path}")
    print("      Open it in Excel; the ElCap ratings calculate automatically.")
    print("      The 'Extraction Audit' sheet shows where each figure came from.")

    if args.json:
        payload = {
            "company_name": fin.company_name,
            "fiscal_year": fin.fiscal_year,
            "statement_basis": fin.statement_basis,
            "unit_scale": fin.unit_scale,
            "source_file": source,
            "line_items_dollars": {
                name: getattr(fin, name) for name in sorted(fin.all_fields())
            },
            "as_reported": fin.as_reported,
            "absent_line_items": fin.absent_line_items,
            "sources": fin.sources,
            "metrics": {m.name: m.value for m in metrics},
            "warnings": warnings,
            "disagreements": disagreements,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)) or ".",
                    exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"      JSON -> {args.json}")

    return out_path


def _print_financials(fin) -> None:
    print("      Read (most recent fiscal year):")
    meta = fin.company_name or ""
    if fin.fiscal_year:
        meta = f"{meta} ({fin.fiscal_year})".strip()
    if meta:
        print(f"        {meta}")
    print(f"        statement basis: {fin.statement_basis}")
    print(f"        statement units: {fin.unit_scale}")
    for name in ("total_revenue", "net_income", "ebit", "current_assets",
                 "current_liabilities", "total_liabilities", "total_equity",
                 "operating_cash_flow"):
        value = getattr(fin, name, None)
        if value is not None:
            print(f"        {name.replace('_', ' '):24s} {value:>18,.0f}")


def _fmt(value) -> str:
    return "(blank)" if value is None else f"{value:,.6f}"


# --------------------------------------------------------------------------
# Interactive menu
# --------------------------------------------------------------------------

def interactive_main(args) -> int:
    print("=" * 62)
    print("  ElCap Scorecard Generator")
    print("  Financial Statement  ->  ElCap Scorecard Excel")
    print("=" * 62)
    while True:
        print()
        print("1) Enter a statement file path")
        print("2) Select a file from the input/ folder")
        print("3) Download a public statement from SEC EDGAR (ticker)")
        print("q) Quit")
        try:
            choice = input("\nChoose [1/2/3/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        if choice in ("q", "4"):
            print("Goodbye.")
            return 0
        if choice == "1":
            source = _prompt_for_path()
        elif choice == "2":
            source = _prompt_from_input_dir()
        elif choice == "3":
            try:
                ticker = input("\nTicker symbol (e.g. CAT, DE): ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            source = _fetch_ticker(ticker) if ticker else None
        else:
            print("Invalid choice.")
            continue

        if not source:
            continue
        try:
            process_file(source, args)
        except Exception as exc:
            print(f"\n[ERROR] {exc}")


def _prompt_for_path() -> str | None:
    raw = input("\nFull path to the statement file: ").strip().strip('"')
    if not raw:
        return None
    if not os.path.isfile(raw):
        print(f"[ERROR] File not found: {raw}")
        return None
    if not raw.lower().endswith(SUPPORTED_EXTENSIONS):
        print(f"[ERROR] Supported types: {', '.join(SUPPORTED_EXTENSIONS)}")
        return None
    return raw


def _prompt_from_input_dir() -> str | None:
    files = _list_input_files()
    if not files:
        print(f"\nNo statement files in {INPUT_DIR} yet.")
        return None
    print("\nFiles found in input/:")
    for i, (name, _path) in enumerate(files, 1):
        print(f"  {i}) {name}")
    raw = input("\nSelect a number (or blank to cancel): ").strip()
    if not raw:
        return None
    try:
        return files[int(raw) - 1][1]
    except (ValueError, IndexError):
        print("[ERROR] Invalid selection.")
        return None


def _list_input_files() -> list[tuple[str, str]]:
    os.makedirs(INPUT_DIR, exist_ok=True)
    return [(name, os.path.join(INPUT_DIR, name))
            for name in sorted(os.listdir(INPUT_DIR))
            if os.path.isfile(os.path.join(INPUT_DIR, name))
            and name.lower().endswith(SUPPORTED_EXTENSIONS)]


if __name__ == "__main__":
    sys.exit(main())
