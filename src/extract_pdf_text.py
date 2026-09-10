"""Extract raw text from a financial statement file.

Supports:
  - PDF  (PyMuPDF, falling back to pdfminer.six for image-based PDFs)
  - HTM / HTML (SEC exhibit format: table rows/cols -> text lines)
  - TXT  (plain text / XBRL summary files)
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Optional, Sequence


class PDFExtractionError(Exception):
    """Raised when a file cannot be read at all."""


def extract_statement_text(path: str, max_pages: Optional[int] = None,
                           pages: Optional[Sequence[int]] = None) -> str:
    """Return the full text of a financial statement by file extension.

    ``pages`` selects 1-based PDF pages; it is ignored for HTML and TXT, which
    have no pagination.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_pdf_text(path, max_pages, pages)
    if ext in (".htm", ".html"):
        return _html_to_text(path)
    if ext == ".txt":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    raise PDFExtractionError(f"Unsupported file type: {path}")


def extract_pdf_text(pdf_path: str, max_pages: Optional[int] = None,
                     pages: Optional[Sequence[int]] = None) -> str:
    """Return the full text of a PDF as a single string.

    Args:
        pdf_path: Path to the PDF file.
        max_pages: If set, only extract text from the first N pages (helpful
            for huge filings). None means extract all pages.
        pages: If set, extract only these 1-based page numbers, in order. This
            is how a user hands over the four to ten pages of an annual report
            that actually carry the statements: everything else in a 100-page
            filing is discussion no metric is computed from, and leaving it in
            only gives the model more chances to read the wrong column.

    Returns:
        The concatenated text of the PDF.
    """
    if not os.path.isfile(pdf_path):
        raise PDFExtractionError(f"File not found: {pdf_path}")
    if not pdf_path.lower().endswith(".pdf"):
        raise PDFExtractionError(f"Not a PDF file: {pdf_path}")

    # Two readers, because each copes with a different kind of broken PDF.
    # Whichever produces usable text first wins; if neither does, the reason
    # each one failed is reported rather than a blanket "probably scanned".
    failures: list[str] = []
    for label, reader in (("PyMuPDF", _extract_with_pymupdf),
                          ("pdfminer.six", _extract_with_pdfminer)):
        try:
            text = reader(pdf_path, max_pages, pages)
        except Exception as exc:  # pragma: no cover - best effort fallback
            failures.append(f"{label}: {exc}")
            continue
        if _looks_like_text(text):
            return text
        failures.append(f"{label}: produced no readable text")

    raise PDFExtractionError(
        "Could not extract readable text from the PDF. It may be a scanned "
        "image PDF (OCR is not included). Details - " + "; ".join(failures)
    )


def _extract_with_pymupdf(pdf_path: str, max_pages: Optional[int],
                          pages: Optional[Sequence[int]] = None) -> str:
    import fitz

    chunks: list[str] = []
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        if pages:
            wanted = [n for n in pages if 1 <= n <= total]
        else:
            limit = total if max_pages is None else min(max_pages, total)
            wanted = list(range(1, limit + 1))
        for number in wanted:
            # Each page is labelled so a figure can be traced back to the page
            # it was printed on, and so a mis-selected page is visible.
            chunks.append(f"[page {number}]\n" + doc[number - 1].get_text("text"))
    return "\n\n".join(chunks)


def _extract_with_pdfminer(pdf_path: str, max_pages: Optional[int],
                           pages: Optional[Sequence[int]] = None) -> str:
    from pdfminer.high_level import extract_text

    if pages:
        return extract_text(pdf_path, page_numbers=[n - 1 for n in pages])
    if max_pages is not None:
        return extract_text(pdf_path, maxpages=max_pages)
    return extract_text(pdf_path)


def _looks_like_text(text: str, min_chars: int = 40) -> bool:
    """Heuristic: a usable extraction has a reasonable amount of characters."""
    text = (text or "").strip()
    if len(text) < min_chars:
        return False
    # Must not be almost entirely blank/whitespace
    non_blank = sum(1 for ch in text if not ch.isspace())
    return non_blank >= min_chars


#: Mean characters per page below which a PDF is treated as scanned. Real
#: audited statements measured 1,885-2,835 chars/page across the test corpus;
#: a PDF with a typed cover page and scanned statements came in at ~103.
MIN_CHARS_PER_PAGE = 250

#: Share of pages with no text above which a PDF is treated as scanned,
#: whatever the average. The average alone is not enough: a wordy cover page
#: in front of scanned statements can lift the mean over the threshold while
#: every figure remains an image. Real statements in the corpus ran 0-8%
#: empty (blank separator pages), so a quarter is a wide margin.
MAX_EMPTY_PAGE_SHARE = 0.25


def scan_report(pdf_path: str) -> dict:
    """Per-page text census for a PDF: how much text each page yields.

    A statement scanned to image carries no text layer, so the pages that
    matter are empty even when the document as a whole looks fine (a typed
    cover page alone clears any whole-document threshold).
    """
    import fitz

    per_page: list[int] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            per_page.append(len(page.get_text("text").strip()))
    pages = len(per_page) or 1
    empty = [i + 1 for i, n in enumerate(per_page) if n < 50]
    return {
        "pages": pages,
        "total_chars": sum(per_page),
        "chars_per_page": sum(per_page) / pages,
        "empty_pages": empty,
    }


def check_text_layer(pdf_path: str) -> Optional[str]:
    """Return a warning if a PDF looks scanned, else None.

    Kept separate from extraction so a caller can decide: a partially scanned
    document still yields its typed pages, and the user needs to be told which
    pages are missing rather than handed a confident scorecard built from a
    cover letter.
    """
    try:
        report = scan_report(pdf_path)
    except Exception:
        return None

    empty, pages = report["empty_pages"], report["pages"]
    sparse = report["chars_per_page"] < MIN_CHARS_PER_PAGE
    mostly_images = len(empty) / pages > MAX_EMPTY_PAGE_SHARE
    if not (sparse or mostly_images):
        return None

    where = (f"{len(empty)} of {pages} pages carry no text"
             if empty else f"only {report['total_chars']:,} characters in total")
    return (
        f"This PDF looks scanned or image-based: {where} "
        f"({report['chars_per_page']:.0f} characters per page, against "
        f"{MIN_CHARS_PER_PAGE}+ for a text PDF). Figures on image-only pages "
        "cannot be read - run the file through OCR first. "
        + (f"Pages with no text: {_summarise_pages(empty)}." if empty else "")
    )


def _summarise_pages(pages: list[int], limit: int = 12) -> str:
    shown = ", ".join(str(p) for p in pages[:limit])
    return shown + (f" (+{len(pages) - limit} more)" if len(pages) > limit else "")


class _TDTableParser(HTMLParser):
    """Collect finance-formatted <table> tables from an EDGAR exhibit.

    Each <tr> becomes one line; cells are tab-separated so the extracted text
    reads like an aligned statement of operations / balance sheet.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.in_table = 0
        self.in_row = False
        self.cell: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t == "table":
            self.in_table += 1
            if self.in_table == 1:
                self.parts.append("\n")
        elif t == "tr" and self.in_table:
            self.in_row = True
            self.cell = []
        elif t == "td" and self.in_row:
            self.cell = []
        elif t in ("br", "p") and self.in_table == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_row:
            self.cell.append(data)
        elif self.in_table == 0:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "td" and self.in_row:
            self.parts.append("".join(self.cell).strip())
            self.parts.append("\t")
        elif t == "tr" and self.in_row:
            if self.parts and self.parts[-1].endswith("\t"):
                self.parts[-1] = self.parts[-1].rstrip("\t")
            self.parts.append("\n")
            self.in_row = False
        elif t == "table" and self.in_table:
            self.in_table -= 1


def _html_to_text(html_path: str) -> str:
    """Convert an SEC exhibit HTML file to readable statement text."""
    with open(html_path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    parser = _TDTableParser()
    try:
        parser.feed(raw)
    except Exception:
        pass

    text = "".join(parser.parts)
    # Collapse runs of whitespace within lines but keep table tabs/newlines
    text = re.sub(r" +", " ", text)
    text = re.sub(r"[\t ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not _looks_like_text(text):
        # No tables found; fall back to a crude tag-strip for prose filings
        text = re.sub(r"(?is)<br\s*/?>|<p\s*/?>|<div\s*/?>|</p>|</div>", "\n", raw)
        text = re.sub(r"(?s)<[^>]+>", "", text)
        text = text.replace("&nbsp;", " ")
        text = re.sub(r" +", " ", text)
    return text


# --------------------------------------------------------------------------
# Finding the statement pages inside a full annual report
#
# An annual report runs 60-200 pages; the figures the scorecard needs live on
# four to ten of them. Asking a user to find those pages by eye is the slowest
# step in the whole workflow, so the pages are classified here and offered as
# a starting selection the user can correct.
# --------------------------------------------------------------------------

#: Headings that identify a primary financial statement. Matching is done on a
#: whitespace-collapsed, lower-cased page, so line breaks inside a heading
#: ("CONSOLIDATED BALANCE\nSHEETS") do not hide it.
PRIMARY_STATEMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Balance sheet", r"balance sheets?\b"),
    ("Balance sheet", r"statements? of financial (position|condition)"),
    ("Balance sheet", r"statements? of assets(,| and) liabilities"),
    ("Income statement", r"statements? of operations"),
    ("Income statement", r"statements? of income\b"),
    ("Income statement", r"\bincome statements?\b"),
    ("Income statement", r"statements? of earnings"),
    ("Income statement", r"statements? of comprehensive (income|loss)"),
    ("Income statement", r"statements? of activities"),
    ("Income statement", r"statements? of revenues? and"),
    ("Cash flow statement", r"statements? of cash flows?"),
    ("Equity statement", r"statements? of changes in (stockholders|shareholders|members|partners|net assets|equity)"),
    ("Equity statement", r"statements? of (stockholders|shareholders|members|partners)[’']? (equity|capital|deficit)"),
    # "Statements of Redeemable Noncontrolling Interests and Equity" and its
    # many cousins: whatever sits between the two words, it is the equity roll.
    ("Equity statement", r"statements? of [a-z, ]{0,60}\bequity\b"),
)

#: Notes that carry line items no primary statement always shows: the debt
#: split the leverage metrics need, and the soft assets tangible net worth
#: deducts. Worth offering, but never on their own.
NOTE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Debt & leases note", r"long[- ]term debt|notes payable|line of credit|credit (facility|agreement)|finance lease|capital lease"),
    ("Goodwill & intangibles note", r"goodwill|intangible assets"),
    ("Income tax note", r"income taxes?\b"),
)

#: Characters from the top of a page within which a statement heading must
#: appear for the page to count as that statement.
HEADING_WINDOW_CHARS = 500

#: A page of tables carries this many figures; prose about them carries far
#: fewer. Used to tell a statement from the table of contents that lists it.
MIN_FIGURES_FOR_A_TABLE = 15

#: Most figures a suggested selection may contain, primary statements first.
MAX_SUGGESTED_PAGES = 14
MAX_SUGGESTED_NOTE_PAGES = 6

_FIGURE_RE = re.compile(r"\(?\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?\$?\d+\.\d{2}\)?")
_TOC_RE = re.compile(r"table of contents|index to (the )?(consolidated )?financial statements")


def page_count(pdf_path: str) -> int:
    """Number of pages in a PDF."""
    import fitz

    with fitz.open(pdf_path) as doc:
        return len(doc)


def page_index(pdf_path: str) -> list[dict]:
    """Classify every page of a PDF by what kind of statement it carries.

    Each entry has the page number, its character and figure counts, the
    statement kinds whose headings appear on it, and whether it looks like a
    contents listing rather than a statement.
    """
    import fitz

    pages: list[dict] = []
    with fitz.open(pdf_path) as doc:
        for number, page in enumerate(doc, start=1):
            raw = page.get_text("text")
            flat = re.sub(r"\s+", " ", raw).lower()
            figures = len(_FIGURE_RE.findall(raw))
            # A statement wears its heading at the top of the page. A note that
            # merely mentions "the balance sheet" in its third paragraph is not
            # a balance sheet, so only the head of the page is searched.
            kinds = _matches(flat[:HEADING_WINDOW_CHARS],
                             PRIMARY_STATEMENT_PATTERNS)
            notes = _matches(flat, NOTE_PATTERNS)
            # A contents page names several statements and almost no figures;
            # a real statement names one and is dense with them.
            is_contents = bool(_TOC_RE.search(flat)) or (
                len(kinds) >= 2 and figures < MIN_FIGURES_FOR_A_TABLE)
            pages.append({
                "page": number,
                "chars": len(raw.strip()),
                "figures": figures,
                "statements": kinds,
                "notes": notes,
                "is_contents": is_contents,
                "is_table": figures >= MIN_FIGURES_FOR_A_TABLE,
            })
    return pages


def _matches(flat_text: str, patterns) -> list[str]:
    found: list[str] = []
    for label, pattern in patterns:
        if label not in found and re.search(pattern, flat_text):
            found.append(label)
    return found


def suggest_statement_pages(pdf_path: str) -> dict:
    """Propose the pages of an annual report that hold the statements.

    Returns ``{"pages": [...], "labels": {page: why}, "index": [...]}``. The
    proposal is a starting point, not a verdict: it is shown to the user with
    the reason for every page so a wrong guess is visible and correctable.
    """
    index = page_index(pdf_path)
    labels: dict[int, str] = {}

    primary = [p for p in index
               if p["statements"] and p["is_table"] and not p["is_contents"]]
    for p in primary:
        labels[p["page"]] = " + ".join(p["statements"])

    # A balance sheet routinely runs onto a second page whose only heading is
    # "(continued)". Take the next page when it is still dense with figures
    # and introduces no statement of its own.
    for p in list(primary):
        nxt = next((q for q in index if q["page"] == p["page"] + 1), None)
        if (nxt and nxt["page"] not in labels and nxt["is_table"]
                and not nxt["statements"] and not nxt["is_contents"]):
            labels[nxt["page"]] = f"continuation of {p['statements'][0].lower()}"

    if labels:
        notes = [p for p in index
                 if p["page"] not in labels and p["notes"] and p["is_table"]
                 and not p["is_contents"]]
        for p in notes[:MAX_SUGGESTED_NOTE_PAGES]:
            labels[p["page"]] = " + ".join(p["notes"])

    pages = sorted(labels)[:MAX_SUGGESTED_PAGES]
    return {"pages": pages,
            "labels": {n: labels[n] for n in pages},
            "index": index}


def parse_page_selection(text: str, maximum: int) -> list[int]:
    """Turn "1-3, 7, 12" into [1, 2, 3, 7, 12], rejecting what cannot exist."""
    selected: list[int] = []
    for chunk in re.split(r"[,\s]+", (text or "").strip()):
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*[-–]\s*(\d+))?", chunk)
        if not match:
            raise ValueError(f"Not a page or range: {chunk!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError(f"Not a valid page range: {chunk!r}")
        if end > maximum:
            raise ValueError(
                f"Page {end} does not exist; the document has {maximum} pages.")
        selected.extend(range(start, end + 1))
    return sorted(set(selected))


def format_page_selection(pages) -> str:
    """Render [1, 2, 3, 7] as "1-3, 7" for display and for the input box."""
    pages = sorted(set(pages))
    if not pages:
        return ""
    groups: list[str] = []
    start = previous = pages[0]
    for number in pages[1:] + [None]:
        if number == previous + 1:
            previous = number
            continue
        groups.append(str(start) if start == previous else f"{start}-{previous}")
        if number is None:
            break
        start = previous = number
    return ", ".join(groups)
