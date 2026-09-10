"""Reading statements out of PDFs, and refusing to guess at scans.

Fixtures are built with PyMuPDF so the suite stays self-contained: a text PDF,
an image-only PDF (a page rendered to a picture, which is what a scanner
produces), and the mixed case that matters most in practice -- a typed cover
page in front of scanned statements.
"""

import os

import pytest

fitz = pytest.importorskip("fitz")

from extract_pdf_text import (MIN_CHARS_PER_PAGE, PDFExtractionError,
                              check_text_layer, extract_statement_text,
                              scan_report)

SAMPLE_PDF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sample", "input", "Audited Financial Statements.pdf")

BODY = ("Total current assets 888,160\nTotal current liabilities 248,149\n"
        "Total revenue 707,868\nNet loss (1,484,458)\n") * 12


def make_text_pdf(path, pages=3):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page().insert_text((40, 60), BODY, fontsize=7)
    doc.save(path)
    doc.close()
    return path


def rasterize(src, path, keep_text_pages=0):
    """Rewrite a PDF so pages become flat images, optionally keeping the
    first few as real text -- the typed-cover-page-plus-scan case."""
    doc, out = fitz.open(src), fitz.open()
    if keep_text_pages:
        out.insert_pdf(doc, from_page=0, to_page=keep_text_pages - 1)
    for i in range(keep_text_pages, len(doc)):
        pix = doc[i].get_pixmap(dpi=72)
        page = out.new_page(width=pix.width, height=pix.height)
        page.insert_image(page.rect, pixmap=pix)
    out.save(path)
    out.close()
    doc.close()
    return path


@pytest.fixture
def text_pdf(tmp_path):
    return make_text_pdf(str(tmp_path / "text.pdf"))


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def test_text_pdf_is_read(text_pdf):
    assert "Total current assets 888,160" in extract_statement_text(text_pdf)


def test_the_sample_statement_still_reads():
    text = extract_statement_text(SAMPLE_PDF)
    assert "Zencoder" in text and "707,868" in text


def test_image_only_pdf_is_rejected(text_pdf, tmp_path):
    scanned = rasterize(text_pdf, str(tmp_path / "scan.pdf"))
    with pytest.raises(PDFExtractionError, match="scanned image PDF"):
        extract_statement_text(scanned)


def test_rejection_names_both_readers(text_pdf, tmp_path):
    """Both readers are tried; the message says what each one hit rather than
    blaming a scan for every failure."""
    scanned = rasterize(text_pdf, str(tmp_path / "scan.pdf"))
    with pytest.raises(PDFExtractionError) as exc:
        extract_statement_text(scanned)
    assert "PyMuPDF" in str(exc.value) and "pdfminer" in str(exc.value)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(PDFExtractionError, match="File not found"):
        extract_statement_text(str(tmp_path / "nope.pdf"))


def test_unsupported_extension_is_reported(tmp_path):
    path = tmp_path / "statement.docx"
    path.write_text("x")
    with pytest.raises(PDFExtractionError, match="Unsupported file type"):
        extract_statement_text(str(path))


# --------------------------------------------------------------------------
# Scan detection
# --------------------------------------------------------------------------

def test_real_statements_are_not_flagged():
    assert check_text_layer(SAMPLE_PDF) is None
    assert scan_report(SAMPLE_PDF)["chars_per_page"] > MIN_CHARS_PER_PAGE


def test_text_pdf_is_not_flagged(text_pdf):
    assert check_text_layer(text_pdf) is None


def test_partial_scan_is_flagged_even_though_it_extracts(text_pdf, tmp_path):
    """The dangerous case: a typed cover page clears any whole-document
    threshold while every statement page is an image."""
    mixed = rasterize(text_pdf, str(tmp_path / "mixed.pdf"), keep_text_pages=1)
    assert extract_statement_text(mixed)          # extraction succeeds ...
    warning = check_text_layer(mixed)             # ... and is still called out
    assert warning and "OCR" in warning
    assert "2 of 3 pages carry no text" in warning


def test_the_warning_names_the_pages_to_ocr(text_pdf, tmp_path):
    mixed = rasterize(text_pdf, str(tmp_path / "mixed.pdf"), keep_text_pages=1)
    assert "Pages with no text: 2, 3" in check_text_layer(mixed)


def test_scan_report_counts_empty_pages(text_pdf, tmp_path):
    mixed = rasterize(text_pdf, str(tmp_path / "mixed.pdf"), keep_text_pages=1)
    report = scan_report(mixed)
    assert report["pages"] == 3
    assert report["empty_pages"] == [2, 3]


def test_check_is_quiet_on_a_file_it_cannot_open(tmp_path):
    """The scan check is advisory; it must never be the thing that fails."""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really a pdf")
    assert check_text_layer(str(broken)) is None


# --------------------------------------------------------------------------
# HTML exhibits
# --------------------------------------------------------------------------

def test_html_tables_become_tab_separated_rows(tmp_path):
    path = tmp_path / "ex99.htm"
    path.write_text(
        "<html><body><table>"
        "<tr><td>Total revenue</td><td>707,868</td><td>341,416</td></tr>"
        "<tr><td>Net loss</td><td>(1,484,458)</td><td>(442,583)</td></tr>"
        "</table></body></html>", encoding="utf-8")
    lines = [l for l in extract_statement_text(str(path)).splitlines() if l.strip()]
    assert lines[0].split("\t") == ["Total revenue", "707,868", "341,416"]


def test_prose_html_without_tables_still_yields_text(tmp_path):
    path = tmp_path / "prose.htm"
    path.write_text("<html><body><p>Total revenue was $707,868 for the year "
                    "ended December 31, 2011.</p></body></html>", encoding="utf-8")
    assert "707,868" in extract_statement_text(str(path))


def test_a_wordy_cover_page_does_not_mask_a_scan(text_pdf, tmp_path):
    """The mean alone is not enough: one text-heavy page in front of scanned
    statements lifts chars/page over the threshold, so the share of pages with
    no text has to carry the decision."""
    from extract_pdf_text import MAX_EMPTY_PAGE_SHARE
    mixed = rasterize(text_pdf, str(tmp_path / "mixed.pdf"), keep_text_pages=1)
    report = scan_report(mixed)
    assert report["chars_per_page"] > MIN_CHARS_PER_PAGE      # mean says "fine"
    assert len(report["empty_pages"]) / report["pages"] > MAX_EMPTY_PAGE_SHARE
    assert check_text_layer(mixed)                            # flagged anyway


def test_a_few_blank_separator_pages_are_tolerated(tmp_path):
    """Real statements carry the odd blank page; that is not a scan."""
    doc = fitz.open()
    for i in range(20):
        page = doc.new_page()
        if i not in (5, 11):
            page.insert_text((40, 60), BODY, fontsize=7)
    path = str(tmp_path / "with_blanks.pdf")
    doc.save(path); doc.close()
    assert check_text_layer(path) is None
