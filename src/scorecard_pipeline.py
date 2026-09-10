"""Run a statement end to end and hand back everything a caller needs.

``main.py`` prints its progress and writes a workbook to ``output/``; a web
request needs the same run to come back as data - the figures, the warnings
and the workbook as bytes - so both callers share this one function and cannot
drift apart in what they compute.

    result = build_scorecard("annual_report.pdf", pages=[47, 48, 49, 50])
    result.workbook_bytes          # the .xlsx, ready to download
    result.metrics                 # the 9 MetricResult objects
    result.financials.sources      # where every figure was read from
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from compute_metrics import (ExtractedFinancials, MetricResult,
                             blank_metric_warnings, compute_metrics,
                             render_metrics_for_excel)
from extract_pdf_text import check_text_layer, extract_statement_text
from fill_excel import generate_output_name, write_metrics_to_template
from llm_extract import (extract_financials, extract_financials_verified,
                         make_runner, resolve_backend)

#: File types the pipeline can read.
SUPPORTED_EXTENSIONS = (".pdf", ".htm", ".html", ".txt")

#: Below this many characters, whatever was selected is not a financial
#: statement - an empty page range, a cover page, a scanned image. Running the
#: model on it wastes a minute to produce nine blanks.
MIN_STATEMENT_CHARS = 400


class PipelineError(Exception):
    """Raised when a statement cannot be turned into a scorecard."""


@dataclass
class ScorecardResult:
    """Everything one run produced, for display or for writing to disk."""

    financials: ExtractedFinancials
    metrics: list[MetricResult]
    values: list                      # the 9 cell values written to D5:L5
    warnings: list[str]
    disagreements: list[str]
    workbook_bytes: bytes
    workbook_name: str
    statement_text: str
    source_name: str
    pages: Optional[list[int]]
    backend: str
    model: Optional[str]
    seconds: float
    scan_warning: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def metric_pairs(self) -> list[tuple[MetricResult, object]]:
        return list(zip(self.metrics, self.values))

    @property
    def blank_metrics(self) -> list[str]:
        return [m.name for m, v in self.metric_pairs if v is None]

    def to_json_dict(self) -> dict:
        """The run as plain JSON - the same payload ``main.py --json`` writes."""
        fin = self.financials
        return {
            "company_name": fin.company_name,
            "fiscal_year": fin.fiscal_year,
            "statement_basis": fin.statement_basis,
            "unit_scale": fin.unit_scale,
            "source_file": self.source_name,
            "pages": self.pages,
            "backend": self.backend,
            "model": self.model,
            "line_items_dollars": {name: getattr(fin, name)
                                   for name in sorted(fin.all_fields())},
            "as_reported": fin.as_reported,
            "absent_line_items": fin.absent_line_items,
            "sources": fin.sources,
            "metrics": {m.name: m.value for m in self.metrics},
            "warnings": self.warnings,
            "disagreements": self.disagreements,
        }


def build_scorecard(
    source_path: str,
    pages: Optional[Sequence[int]] = None,
    cli: str = "auto",
    model: Optional[str] = None,
    verify: bool = False,
    template_path: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> ScorecardResult:
    """Read a statement, compute the 9 metrics and fill the scorecard.

    Args:
        source_path: A .pdf / .htm / .html / .txt statement on disk.
        pages: 1-based PDF pages to read; None reads the whole document.
        cli: Backend selection - 'auto', 'api', or a CLI name.
        model: Model id passed to the backend.
        verify: Read the statement twice and report where the passes differ.
        template_path: Scorecard template to fill; None uses the bundled one.
        progress: Called with a short status line before each stage.

    Returns:
        A :class:`ScorecardResult`.

    Raises:
        PipelineError: the file is unusable, or nothing readable was selected.
    """
    say = progress or (lambda _message: None)
    started = time.time()

    extension = os.path.splitext(source_path)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise PipelineError(
            f"{extension or 'That file type'} is not supported. Upload a PDF, "
            "an SEC HTML exhibit, or a text file.")

    say("Reading the document")
    page_list = sorted({int(p) for p in pages}) if pages else None
    try:
        text = extract_statement_text(source_path, pages=page_list)
    except Exception as exc:
        raise PipelineError(str(exc)) from exc

    if len(text.strip()) < MIN_STATEMENT_CHARS:
        where = (f"Pages {_render(page_list)} carry" if page_list
                 else "This document carries")
        raise PipelineError(
            f"{where} almost no text ({len(text.strip()):,} characters). Check "
            "the page selection, or run the file through OCR if the statements "
            "are scanned images.")

    scan_warning = check_text_layer(source_path) if extension == ".pdf" else None

    backend = resolve_backend(cli)
    say(f"Reading the line items with {make_runner(cli, model).describe()}")
    if verify:
        financials, disagreements = extract_financials_verified(
            text, model=model, cli=cli)
    else:
        financials = extract_financials(text, model=model, cli=cli)
        disagreements = []

    say("Computing the 9 credit metrics")
    metrics = compute_metrics(financials)
    values = render_metrics_for_excel(metrics)

    warnings = financials.validate() + blank_metric_warnings(metrics)
    if scan_warning:
        warnings.insert(0, scan_warning)

    say("Writing the Excel scorecard")
    workbook_name = generate_output_name(source_path)
    with tempfile.TemporaryDirectory() as staging:
        staged = os.path.join(staging, workbook_name)
        write_metrics_to_template(
            values, staged,
            template_path=template_path,
            financials=financials, metrics=metrics, source_file=source_path,
            warnings=warnings, disagreements=disagreements,
        )
        with open(staged, "rb") as handle:
            workbook_bytes = handle.read()

    return ScorecardResult(
        financials=financials,
        metrics=metrics,
        values=values,
        warnings=warnings,
        disagreements=disagreements,
        workbook_bytes=workbook_bytes,
        workbook_name=workbook_name,
        statement_text=text,
        source_name=os.path.basename(source_path),
        pages=page_list,
        backend=backend,
        model=model,
        seconds=time.time() - started,
        scan_warning=scan_warning,
        notes=list(financials.notes),
    )


def _render(pages: Optional[Sequence[int]]) -> str:
    from extract_pdf_text import format_page_selection

    return format_page_selection(pages or [])


__all__ = ["MIN_STATEMENT_CHARS", "PipelineError", "ScorecardResult",
           "SUPPORTED_EXTENSIONS", "build_scorecard"]
