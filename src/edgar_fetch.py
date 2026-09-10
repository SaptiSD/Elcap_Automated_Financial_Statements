"""Download audited financial statement PDFs from SEC EDGAR by ticker/CIK.

Two strategies:
  1. Full-text search for explicit "AUDITED FINANCIAL STATEMENTS" exhibits
     (EX-99.x) — the same format as the Zencoder sample (acquisition targets).
  2. XBRL fallback: pull the company's US-GAAP facts from the SEC `companyfacts`
     API and build a clean statement text file covering the most recent full
     fiscal year. Works for any public company that files structured data.

Both produce a file in the input folder so the rest of the pipeline is
unchanged.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Optional

import requests

USER_AGENT = "ElcapFinancialExtractor/1.0 (financial statement parsing tool) elcap@example.com"
FTS_URL = ("https://efts.sec.gov/LATEST/search-index?q={query}"
           "&dateRange=custom&startdt={start}&enddt={end}&forms={forms}")

# Mapping of companyfacts node tag -> our line item key.
# Some companies report the finance/operating lease split differently; we
# provide alternates and aggregate where needed.
XBRL_TAGS = {
    "total_revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "Revenue",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "net_income": ["NetIncomeLoss"],
    "ebit": [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "taxes": [
        "IncomeTaxExpenseBenefit",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseOperating",
        "InterestAndDebtExpense",
        "InterestExpenseIncludingAmortizationOfDebtIssuanceCosts",
        "InterestExpenseDebt",
        "InterestExpenseDebtExcludingAmortization",
        "InterestCostsIncurred",
    ],
    "depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationOfOilAndGasProperties",
        "Depreciation",
    ],
    "amortization": ["AmortizationOfIntangibleAssets"],
    "assets_current": ["AssetsCurrent", "CurrentAssets"],
    "liabilities_current": ["LiabilitiesCurrent", "CurrentLiabilities"],
    "total_liabilities": ["Liabilities"],
    "total_equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "goodwill": ["Goodwill"],
    "intangibles": [
        "FiniteLivedIntangibleAssetsNet",
        "IndefiniteLivedIntangibleAssetsExcludingGoodwill",
        "IntangibleAssetsNetExcludingGoodwill",
        "IntangibleAssetsNetIncludingGoodwill",
    ],
    "line_of_credit": [
        "LinesOfCreditCurrent",
        "DebtLineOfCreditCurrent",
        "CommercialPaper",
    ],
    "current_lt_debt": [
        "CurrentPortionOfLongTermDebt",
        "LongTermDebtCurrent",
        "DebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseLiabilityNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    ],
    "current_finance_leases": ["FinanceLeaseLiabilityCurrent"],
    "long_term_finance_leases": ["FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiability"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
}


class EDGARError(Exception):
    """Raised for EDGAR lookup/download failures."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_financial_statement(
    ticker: str, output_dir: str
) -> tuple[str, str]:
    """Fetch financial statement material for a ticker.

    Args:
        ticker: Ticker symbol (or CIK).
        output_dir: Directory to save the downloaded file.

    Returns:
        (file_path, kind) where kind is 'pdf'/'htm'/'xbrl_text'.
    """
    cik, company = _get_cik(ticker)
    print(f"[EDGAR] Found {company} (CIK {cik})")

    # 1) Try an explicit audited-statement exhibit (same format as sample)
    doc_url = _find_exhibit_url(cik, company_name=company, ticker=ticker)
    if doc_url:
        path = _download_url(doc_url, output_dir, ticker)
        if path.lower().endswith((".pdf", ".htm", ".html")):
            print(f"[EDGAR] Downloaded exhibit: {path}")
            return path, ("pdf" if path.lower().endswith(".pdf") else "htm")

    # 2) XBRL companyfacts fallback
    print("[EDGAR] No audited-statement exhibit found; using XBRL companyfacts.")
    text_path = _build_xbrl_statement_text(cik, ticker, output_dir)
    return text_path, "xbrl_text"


def _get_cik(ticker: str) -> tuple[str, str]:
    ticker = ticker.strip()
    if ticker.isdigit():
        return ticker.zfill(10).lstrip("0"), ticker
    ticker = ticker.upper()
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={urllib.parse.quote(ticker)}&type=10-K&dateb=&owner=include"
        "&count=1&output=atom"
    )
    resp = _session().get(url, timeout=30)
    if resp.status_code != 200:
        raise EDGARError(f"CIK lookup failed for {ticker} (HTTP {resp.status_code})")
    m = re.search(r"<cik>(\d+)</cik>", resp.text)
    if not m:
        raise EDGARError(f"Ticker {ticker!r} not found on EDGAR.")
    cik = m.group(1)
    m2 = re.search(r"<conformed-name>([^<]+)</conformed-name>", resp.text)
    return cik, (m2.group(1) if m2 else ticker)


def _find_exhibit_url(cik: str, company_name: str = "", ticker: str = "") -> Optional[str]:
    """Search EDGAR for EX-99.* audited financial-statement exhibits.

    FTS query hits are document-level; each hit has `adsh` (filing accession),
    `file_type` (e.g. EX-99.1) and `file_description`. We pick the best exhibit
    hit and resolve its download filename via the filing's index.json.
    """
    cik_clean = cik.lstrip("0")
    keywords = (("AUDITED", "FINANCIAL STATEMENT"), ("FINANCIAL STATEMENT",))
    for query in _fts_queries(company_name, ticker):
        url = FTS_URL.format(
            query=urllib.parse.quote(query),
            start="2005-01-01",
            end="2026-12-31",
            forms="",
        )
        try:
            resp = _session().get(url, timeout=30)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except (json.JSONDecodeError, requests.RequestException):
            continue

        candidates = []
        for hit in data.get("hits", {}).get("hits", [])[:20]:
            src = hit.get("_source", {})
            hit_ciks = [str(c).lstrip("0") for c in (src.get("ciks") or [])]
            if cik_clean not in hit_ciks:
                continue
            ftype = (src.get("file_type") or "").upper()
            fdesc = (src.get("file_description") or "").upper()
            if not ftype.startswith("EX-99"):
                continue
            if not any(kw in fdesc for kw in ("FINANCIAL STATEMENT", "AUDITED")):
                continue
            score = 0
            for i, words in enumerate(keywords):
                if all(w in fdesc for w in words):
                    score += 3 - i
            candidates.append((score, src.get("adsh", "")))

        candidates.sort(key=lambda t: t[0], reverse=True)
        for _, adsh in candidates:
            if not adsh:
                continue
            exhibit_url = _resolve_exhibit_url(cik_clean, adsh)
            if exhibit_url:
                return exhibit_url
    return None


def _fts_queries(company_name: str, ticker: str) -> list[str]:
    queries = []
    if company_name:
        queries.append(f'"{company_name}" "FINANCIAL STATEMENTS"')
    if ticker:
        queries.append(f'"{ticker}" "FINANCIAL STATEMENTS"')
    if company_name:
        queries.append(f'"{company_name}" "AUDITED"')
    if not queries:
        queries = ['"FINANCIAL STATEMENTS" "AUDITED"']
    return queries


def _resolve_exhibit_url(cik: str, adsh: str) -> Optional[str]:
    """Map an FTS exhibit hit (adsh) to a concrete download URL.

    The FTS hit tells us the exhibit type (EX-99.x). The filing's index.json
    lists the physical filenames. We match them by the exhibit-number encoded in
    the filename (e.g. `d425019dex991.htm` == EX-99.1). Falls back to the first
    plausible exhibit PDF when no clean match exists.
    """
    adsh_no_dash = adsh.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_no_dash}"
    idx_url = f"{base}/index.json"
    try:
        resp = _session().get(idx_url, timeout=30)
        if resp.status_code != 200:
            return None
        idx = resp.json()
    except (json.JSONDecodeError, requests.RequestException):
        return None

    names = []
    for item in idx.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name.lower().endswith((".pdf", ".htm", ".html")):
            names.append(name)

    if not names:
        return None

    # Patterns that encode the exhibit number, highest-specificity first.
    patterns = (
        re.compile(r"dex09?9\.0?(\d)$", re.I),   # d...dex991.htm
        re.compile(r"ex99[\s_.-]*0?(\d)", re.I),  # ex99-1, ex99_1
        re.compile(r"e?x?99[._-]?(\d)", re.I),
    )
    for name in names:
        for pat in patterns:
            m = pat.search(name)
            if m and m.group(1) == "1":
                return f"{base}/{name}"

    # No numeric match: prefer PDFs, then largest-name exhibit.
    pdfs = [n for n in names if n.lower().endswith(".pdf")]
    if pdfs:
        return f"{base}/{pdfs[0]}"
    return f"{base}/{names[0]}"


def _download_url(url: str, output_dir: str, prefix: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    ext = ".pdf" if ".pdf" in url.lower() else ".htm"
    path = os.path.join(output_dir, f"{prefix}_edgar{ext}")
    resp = _session().get(url, timeout=180)
    resp.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(resp.content)
    return path


#: How far a balance-sheet date may sit from the fiscal year end and still be
#: treated as the same period (filers' instants land on the exact FYE, but a
#: few days of drift shows up around 52/53-week calendars).
PERIOD_TOLERANCE_DAYS = 10

#: Statement layout of the generated text file: (section, label, facts key).
XBRL_STATEMENT_LAYOUT: tuple[tuple[str, str, str], ...] = (
    ("STATEMENT OF OPERATIONS", "Total revenue", "total_revenue"),
    ("STATEMENT OF OPERATIONS", "Net income", "net_income"),
    ("STATEMENT OF OPERATIONS", "EBIT / Operating income", "ebit"),
    ("STATEMENT OF OPERATIONS", "Interest expense", "interest_expense"),
    ("STATEMENT OF OPERATIONS", "Income tax expense", "taxes"),
    ("STATEMENT OF OPERATIONS", "Depreciation", "depreciation"),
    ("STATEMENT OF OPERATIONS", "Amortization", "amortization"),
    ("BALANCE SHEET", "Current assets", "assets_current"),
    ("BALANCE SHEET", "Current liabilities", "liabilities_current"),
    ("BALANCE SHEET", "Total liabilities", "total_liabilities"),
    ("BALANCE SHEET", "Stockholders' equity", "total_equity"),
    ("BALANCE SHEET", "Goodwill", "goodwill"),
    ("BALANCE SHEET", "Intangible assets", "intangibles"),
    ("BALANCE SHEET", "Line of credit", "line_of_credit"),
    ("BALANCE SHEET", "Current LT debt", "current_lt_debt"),
    ("BALANCE SHEET", "Long-term debt", "long_term_debt"),
    ("BALANCE SHEET", "Current finance leases", "current_finance_leases"),
    ("BALANCE SHEET", "Long-term finance leases", "long_term_finance_leases"),
    ("CASH FLOW", "Operating cash flow", "operating_cash_flow"),
)

#: Text used when a filer tags no value for a line item. Deliberately explicit:
#: "None" reads like zero, and the extractor must treat it as unknown instead.
NOT_REPORTED = "not tagged in this filer's XBRL data (unknown - do NOT treat as zero)"

#: Text used when the only tagged value belongs to an earlier fiscal year.
STALE = ("last tagged as {amount:,.0f} as of {when}, which is NOT this fiscal "
         "year (unknown for this year - do NOT use this figure)")


def _build_xbrl_statement_text(cik: str, ticker: str, output_dir: str) -> str:
    """Fetch US-GAAP facts and write a statement-like text file.

    Returns the output file path. The file resembles a financial statement
    with the most recent annual figures, so the LLM extractor can consume it
    exactly like a parsed PDF.

    All line items are pinned to one fiscal year end so a freshly filed 10-Q
    cannot pull the balance sheet a quarter ahead of the income statement.
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
    resp = _session().get(url, timeout=60)
    if resp.status_code != 200:
        raise EDGARError(f"XBRL companyfacts failed (HTTP {resp.status_code})")
    data = resp.json()

    # The income statement sets the reporting period everything else follows.
    period_end = None
    for anchor in ("net_income", "total_revenue"):
        _value, end = _pick_annual_fact(data, XBRL_TAGS[anchor])
        if end:
            period_end = end
            break

    facts: dict[str, Optional[float]] = {}
    stale: dict[str, tuple[float, str]] = {}
    for key, tags in XBRL_TAGS.items():
        value, end = _pick_annual_fact(data, tags, target_end=period_end)
        on_period = (value is not None and end is not None and period_end is not None
                     and _within_days(end, period_end, PERIOD_TOLERANCE_DAYS))
        if value is not None and period_end and not on_period:
            # A figure from another year would silently distort the ratios, so
            # it is reported as stale rather than used.
            stale[key] = (value, end or "unknown date")
            value = None
        facts[key] = value

    if facts.get("total_revenue") is None and facts.get("net_income") is None:
        raise EDGARError(
            "XBRL facts missing for this company; it may not file structured"
            " US-GAAP data on EDGAR."
        )

    lines = [
        f"Financial statement data (XBRL companyfacts) for {ticker} (CIK {cik})",
        f"Fiscal year ended: {period_end or 'unknown'}",
        "All figures are in whole US dollars (unit_scale: raw).",
    ]
    section = None
    for sect, label, key in XBRL_STATEMENT_LAYOUT:
        if sect != section:
            section = sect
            lines += ["", sect]
        value = facts[key]
        if value is not None:
            lines.append(f"{label}: {value:,.0f}")
        elif key in stale:
            amount, when = stale[key]
            lines.append(f"{label}: {STALE.format(amount=amount, when=when)}")
        else:
            lines.append(f"{label}: {NOT_REPORTED}")

    text = "\n".join(lines)
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{ticker}_xbrl_statement.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _pick_annual_fact(
    data: dict, tags: list[str], target_end: Optional[str] = None
) -> tuple[Optional[float], Optional[str]]:
    """Return (value, period end) for the best annual fact among ``tags``.

    An "annual" fact is one where either ``fp == "FY"`` or the reported period
    spans roughly a full fiscal year (which catches figures reported in proxy
    statements, where fp/fy are often left blank).

    When ``target_end`` is given, a fact at that period end wins outright, so
    every line item in the generated statement describes the same fiscal year.
    Otherwise the latest ``end`` date wins, falling back to the latest fact of
    any period if nothing looks annual.
    """
    gaap = data.get("facts", {}).get("us-gaap", {})
    annual: list[tuple[str, float]] = []
    fallback: list[tuple[str, float]] = []

    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        for unit, entries in node.get("units", {}).items():
            if unit != "USD":
                continue
            for entry in entries:
                value = entry.get("val")
                if value is None:
                    continue
                end = entry.get("end") or "0000-00-00"
                fallback.append((end, value))
                if entry.get("fp") == "FY" or _is_full_year(entry):
                    annual.append((end, value))

    if target_end:
        on_period = [(end, val) for end, val in annual
                     if _within_days(end, target_end, PERIOD_TOLERANCE_DAYS)]
        if on_period:
            on_period.sort()
            return on_period[-1][1], on_period[-1][0]

    for candidates in (annual, fallback):
        if candidates:
            candidates.sort()
            return candidates[-1][1], candidates[-1][0]
    return None, None


def _within_days(end: str, target: str, tolerance: int) -> bool:
    """True if two ISO dates are within ``tolerance`` days of each other."""
    from datetime import date

    try:
        return abs((date.fromisoformat(end)
                    - date.fromisoformat(target)).days) <= tolerance
    except ValueError:
        return False


def _is_full_year(e: dict) -> bool:
    """True if the entry's period spans roughly a full year."""
    from datetime import date

    start, end = (e.get("start") or ""), (e.get("end") or "")
    if not start or not end:
        return False
    try:
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end)
    except ValueError:
        return False
    days = (d1 - d0).days
    return 320 <= days <= 390


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: edgar_fetch.py <TICKER> [output_dir]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else "."
    path, kind = fetch_financial_statement(sys.argv[1], out)
    print("Saved:", path, f"({kind})")