"""Populate a copy of the ElCap scorecard template with computed metrics.

The template keeps all formulas/rating logic intact; we only overwrite the
9 financial-value input cells in the "Scorecard to ElCap" sheet (row 5,
columns D through L) and append an "Extraction Audit" sheet recording where
each figure came from.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font

# Column letters for the 9 metrics on row 5 of "Scorecard to ElCap"
METRIC_COLUMNS = ["D", "E", "F", "G", "H", "I", "J", "K", "L"]
METRIC_ROW = 5
TARGET_SHEET = "Scorecard to ElCap"
AUDIT_SHEET = "Extraction Audit"

#: How the extractor's statement_basis reads on the audit sheet.
BASIS_LABELS = {
    "complete": "complete financial statements",
    "abbreviated": "ABBREVIATED presentation - review before scoring",
    "unknown": "could not be determined",
}

#: Line items shown on the audit sheet, in statement order.
AUDIT_LINE_ITEMS: tuple[tuple[str, str], ...] = (
    ("total_revenue", "Total revenue"),
    ("ebit", "EBIT / operating income"),
    ("net_income", "Net income (loss)"),
    ("interest_expense", "Interest expense (negative = net interest income)"),
    ("taxes", "Income taxes"),
    ("depreciation", "Depreciation"),
    ("amortization", "Amortization"),
    ("current_assets", "Total current assets"),
    ("current_liabilities", "Total current liabilities"),
    ("total_liabilities", "Total liabilities"),
    ("total_equity", "Total equity"),
    ("goodwill", "Goodwill"),
    ("intangible_assets", "Intangible assets"),
    ("line_of_credit", "Line of credit outstanding"),
    ("current_portion_lt_debt", "Current portion of long-term debt"),
    ("current_finance_leases", "Current portion of finance leases"),
    ("long_term_debt", "Long-term debt"),
    ("long_term_finance_leases", "Long-term finance leases"),
    ("operating_cash_flow", "Net cash from operating activities"),
)


def _default_template_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.abspath(os.path.join(here, "..")),
                        "template", "ElCap Scorecard Template.xlsx")


def find_template(template_path: Optional[str] = None) -> str:
    path = template_path or _default_template_path()
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Template workbook not found: {path}. Expected an ElCap "
            "scorecard xlsx with a 'Scorecard to ElCap' sheet."
        )
    return path


def write_metrics_to_template(
    metric_values: list,
    output_path: str,
    template_path: Optional[str] = None,
    financials=None,
    metrics=None,
    source_file: Optional[str] = None,
    warnings: Optional[list[str]] = None,
    disagreements: Optional[list[str]] = None,
) -> str:
    """Write the 9 metric values into a copy of the template.

    Args:
        metric_values: 9 values (Revenue, EBIT Margin, DSCR, EBITDA/Interest,
            Current Ratio, OCF Ratio, Funded Debt/EBITDA, Debt/TNW, OCF/Debt).
            None entries are written as blank cells so the template's rating
            formulas see an empty input rather than stale data.
        output_path: Where to save the generated workbook.
        template_path: Optional path to the template workbook.
        financials: Optional ``ExtractedFinancials`` used to build the audit
            sheet. Without it the audit sheet is skipped.
        metrics: Optional list of ``MetricResult`` for the audit sheet.
        source_file: Path of the statement the figures were read from.
        warnings: Validation warnings to record on the audit sheet.
        disagreements: Line items two extraction passes disagreed on.

    Returns:
        The output path.
    """
    if len(metric_values) != 9:
        raise ValueError(f"Expected 9 metric values, got {len(metric_values)}")

    template_path = find_template(template_path)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    wb = load_workbook(template_path, data_only=False, keep_vba=False)
    if TARGET_SHEET not in wb.sheetnames:
        raise ValueError(
            f"Template sheet '{TARGET_SHEET}' not found in {template_path}"
        )
    ws = wb[TARGET_SHEET]

    for col_letter, value in zip(METRIC_COLUMNS, metric_values):
        cell = ws[f"{col_letter}{METRIC_ROW}"]
        if value is None:
            # A blank means "not computable". Writing an empty string clears
            # whatever the template held so no stale figure can be scored.
            cell.value = None
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Metric value for column {col_letter} is not numeric: {value!r}"
            ) from exc
        cell.value = None if numeric != numeric else numeric  # NaN -> blank

    if financials is not None:
        _write_audit_sheet(wb, financials, metrics or [], source_file,
                           warnings or [], disagreements or [])

    wb.save(output_path)
    return output_path


def _write_audit_sheet(wb, fin, metrics, source_file, warnings, disagreements):
    """Record the provenance of every extracted figure on its own sheet."""
    if AUDIT_SHEET in wb.sheetnames:
        del wb[AUDIT_SHEET]
    ws = wb.create_sheet(AUDIT_SHEET)

    bold = Font(bold=True)
    header = Font(bold=True, size=12)
    wrap = Alignment(wrap_text=True, vertical="top")
    row = 1

    def put(r, values, font=None):
        for i, value in enumerate(values, start=1):
            cell = ws.cell(r, i, value)
            if font:
                cell.font = font
            cell.alignment = wrap
        return r + 1

    row = put(row, ["ElCap Scorecard - Extraction Audit"], header)
    row += 1
    for label, value in (
        ("Company", fin.company_name or "(not identified)"),
        ("Fiscal year", fin.fiscal_year or "(not identified)"),
        ("Statement basis", BASIS_LABELS.get(fin.statement_basis, fin.statement_basis)),
        ("Statement units", f"as printed, in {fin.unit_scale}"),
        ("Source file", os.path.basename(source_file) if source_file else ""),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ):
        row = put(row, [label, value], bold if label == "Company" else None)
        ws.cell(row - 1, 1).font = bold
    row += 1

    row = put(row, ["Line items read from the statement"], header)
    row = put(row, ["Line item", "As printed", "In dollars", "Status",
                    "Source in statement"], bold)
    for field_name, label in AUDIT_LINE_ITEMS:
        dollars = getattr(fin, field_name, None)
        printed = fin.as_reported.get(field_name)
        if field_name in fin.absent_line_items:
            status = "not on statement (treated as 0)"
        elif dollars is None:
            status = "NOT FOUND - metric left blank"
        else:
            status = "read from statement"
        row = put(row, [label, printed, dollars, status,
                        fin.sources.get(field_name, "")])
    row += 1

    if metrics:
        row = put(row, ["Computed metrics"], header)
        row = put(row, ["Metric", "Value", "Note"], bold)
        for m in metrics:
            row = put(row, [m.name,
                            m.value if m.value is not None else "(blank)",
                            m.detail])
        row += 1

    for title, lines in (
        ("Validation warnings", warnings),
        ("Second-pass disagreements", disagreements),
        ("Extractor notes", list(fin.notes)),
    ):
        if not lines:
            continue
        row = put(row, [title], header)
        for line in lines:
            row = put(row, ["", line])
        row += 1

    for column, width in zip("ABCDE", (44, 18, 18, 32, 70)):
        ws.column_dimensions[column].width = width
    return ws


def generate_output_name(source_pdf: str, suffix: str = "") -> str:
    """Create a timestamped output filename from the source file's name."""
    base = os.path.splitext(os.path.basename(source_pdf))[0]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base}_{stamp}{suffix}.xlsx"


def default_output_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "output"))


def read_scorecard_values(path: str) -> list:
    """Read back the 9 values in D5:L5 of a generated workbook (for tests)."""
    wb = load_workbook(path, data_only=False)
    ws = wb[TARGET_SHEET]
    return [ws[f"{c}{METRIC_ROW}"].value for c in METRIC_COLUMNS]


__all__ = [
    "AUDIT_SHEET", "BASIS_LABELS", "METRIC_COLUMNS", "METRIC_ROW", "TARGET_SHEET",
    "default_output_dir", "find_template", "generate_output_name",
    "read_scorecard_values", "write_metrics_to_template",
]
