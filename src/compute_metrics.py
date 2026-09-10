"""Compute the 9 ElCap credit metrics from extracted financial line items.

Metric definitions follow the "Calc Definitions" sheet in the ElCap scorecard
workbook:

  1. Revenue              - 12-month total revenue
  2. EBIT Margin          - EBIT / Revenue
  3. DSCR                 - (Net Income + Depreciation + Interest Expense)
                            / (Current portion of LTD + current finance leases
                            + Interest Expense)
  4. EBITDA / Interest    - EBITDA / Interest Expense
  5. Current Ratio        - Current Assets / Current Liabilities
  6. OCF Ratio            - Operating Cash Flow / Current Liabilities
  7. Funded Debt / EBITDA - Borrowed Funds / EBITDA
                            Borrowed Funds = LOC + current LTD + current fin
                            leases + long-term debt + long-term fin leases
  8. Debt / TNW           - Total Liabilities / Tangible Net Worth
  9. OCF / Debt           - Operating Cash Flow / Total Liabilities

EBITDA = Net Income + Interest + Taxes + Depreciation + Amortization.

Units
-----
Statements are usually printed "in thousands" or "in millions".  The extractor
returns figures exactly as printed together with a ``unit_scale``; this module
normalises everything to **full dollars** before computing, because the
scorecard's Revenue rating bands are absolute dollar amounts ($10MM - $350MM).

Absent vs. unknown
------------------
A ``None`` line item means "not determined".  A line item the extractor
positively confirmed is *not on the statement* arrives as ``0.0`` (via
``absent_line_items``).  The distinction matters: a company with no debt has
Funded Debt of 0, whereas a company whose debt we failed to read must be left
blank rather than scored as debt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: Multiplier from a statement's printed units to full dollars.
UNIT_SCALES: dict[str, float] = {
    "raw": 1.0,
    "units": 1.0,
    "dollars": 1.0,
    "ones": 1.0,
    "thousands": 1_000.0,
    "millions": 1_000_000.0,
    "billions": 1_000_000_000.0,
}

#: Line items that are monetary and therefore subject to unit scaling.
MONETARY_FIELDS: tuple[str, ...] = (
    "total_revenue", "net_income", "interest_expense", "taxes",
    "depreciation", "amortization", "ebit", "current_assets",
    "current_liabilities", "total_liabilities", "total_equity",
    "intangible_assets", "goodwill", "line_of_credit",
    "current_portion_lt_debt", "current_finance_leases",
    "long_term_debt", "long_term_finance_leases", "operating_cash_flow",
)

#: Debt components that make up Borrowed Funds (metric 7).
DEBT_FIELDS: tuple[str, ...] = (
    "line_of_credit", "current_portion_lt_debt", "current_finance_leases",
    "long_term_debt", "long_term_finance_leases",
)

#: EBITDA add-backs on top of net income.
EBITDA_ADDBACKS: tuple[str, ...] = (
    "interest_expense", "taxes", "depreciation", "amortization",
)

#: Recognised values of ``ExtractedFinancials.statement_basis``.
STATEMENT_BASES: frozenset[str] = frozenset({"complete", "abbreviated", "unknown"})

#: Items every complete set of financial statements carries. None of them
#: present means the document is not a full set of statements.
BALANCE_SHEET_CORE: tuple[str, ...] = (
    "current_assets", "current_liabilities", "total_liabilities",
    "total_equity", "operating_cash_flow",
)

#: Annual revenue no real company reaches; above this the units were applied
#: twice (the world's largest filers are around $700bn).
IMPLAUSIBLE_REVENUE = 1_000_000_000_000.0

#: Bottom of the scorecard's Revenue rating bands ("Scorecard to ElCap" P34).
SCORECARD_MIN_REVENUE = 10_000_000.0


class MetricComputeError(Exception):
    """Raised when required inputs are missing/implausible."""


@dataclass
class ExtractedFinancials:
    """Structured financial line items (most recent fiscal year).

    All monetary attributes are in **full dollars** once :meth:`from_dict`
    has applied ``unit_scale``.
    """

    # Income statement
    total_revenue: Optional[float] = None
    net_income: Optional[float] = None
    interest_expense: Optional[float] = None
    taxes: Optional[float] = None
    depreciation: Optional[float] = None
    amortization: Optional[float] = None
    ebit: Optional[float] = None

    # Balance sheet
    current_assets: Optional[float] = None
    current_liabilities: Optional[float] = None
    total_liabilities: Optional[float] = None
    total_equity: Optional[float] = None
    intangible_assets: Optional[float] = None
    goodwill: Optional[float] = None
    line_of_credit: Optional[float] = None
    current_portion_lt_debt: Optional[float] = None
    current_finance_leases: Optional[float] = None
    long_term_debt: Optional[float] = None
    long_term_finance_leases: Optional[float] = None

    # Cash flow
    operating_cash_flow: Optional[float] = None

    # Provenance
    unit_scale: str = "raw"
    fiscal_year: Optional[str] = None
    company_name: Optional[str] = None
    #: "complete" | "abbreviated" | "unknown" - see STATEMENT_BASES.
    statement_basis: str = "unknown"
    notes: list[str] = field(default_factory=list)
    #: Fields the extractor confirmed are absent from the statement (-> 0.0).
    absent_line_items: list[str] = field(default_factory=list)
    #: field name -> verbatim statement label/value the figure came from.
    sources: dict[str, str] = field(default_factory=dict)
    #: Figures exactly as printed, before unit scaling (for the audit trail).
    as_reported: dict[str, Optional[float]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractedFinancials":
        """Build from the extractor's JSON, applying units and absent flags."""
        unit_scale = normalise_unit_scale(data.get("unit_scale"))
        multiplier = UNIT_SCALES[unit_scale]

        absent = [f for f in _string_list(data.get("absent_line_items"))
                  if f in cls.all_fields()]

        as_reported: dict[str, Optional[float]] = {}
        kwargs: dict[str, Optional[float]] = {}
        for name in MONETARY_FIELDS:
            value = _to_float(data.get(name))
            if value is None and name in absent:
                value = 0.0
            as_reported[name] = value
            kwargs[name] = None if value is None else value * multiplier

        obj = cls(**kwargs)
        obj.unit_scale = unit_scale
        obj.absent_line_items = absent
        obj.as_reported = as_reported
        obj.fiscal_year = str(data.get("fiscal_year") or "") or None
        obj.company_name = str(data.get("company_name") or "") or None
        basis = str(data.get("statement_basis") or "unknown").strip().lower()
        obj.statement_basis = basis if basis in STATEMENT_BASES else "unknown"
        obj.notes = _string_list(data.get("notes") or data.get("warnings"))
        raw_sources = data.get("sources") or {}
        if isinstance(raw_sources, dict):
            obj.sources = {k: str(v) for k, v in raw_sources.items()
                           if k in cls.all_fields() and v}
        return obj

    @staticmethod
    def all_fields() -> set[str]:
        return set(MONETARY_FIELDS)

    # -- derived quantities -------------------------------------------------

    @property
    def interest_expense_positive(self) -> Optional[float]:
        """Interest EXPENSE as a non-negative number.

        Negative values (i.e. net interest *income*) map to ``None`` for
        coverage purposes, since a company reporting interest income has no
        interest expense to 'cover'.
        """
        if self.interest_expense is None or self.interest_expense < 0:
            return None
        return self.interest_expense

    @property
    def ebitda(self) -> Optional[float]:
        """EBITDA = Net Income + Interest Expense + Taxes + Depr + Amort.

        Net income is required.  The four add-backs each default to 0 when
        missing -- an omitted amortization line means no amortization, and
        treating the whole of EBITDA as unknown would needlessly blank two
        metrics.  :meth:`validate` reports which add-backs were assumed zero.
        """
        if self.net_income is None:
            return None
        return self.net_income + sum(
            getattr(self, name) or 0.0 for name in EBITDA_ADDBACKS)

    @property
    def ebitda_assumed_zero(self) -> list[str]:
        """EBITDA add-backs that were missing and treated as zero."""
        return [name for name in EBITDA_ADDBACKS if getattr(self, name) is None]

    @property
    def ebit_effective(self) -> Optional[float]:
        """Reported EBIT (operating income) or Net Income + Interest + Taxes.

        An ``ebit`` of exactly zero is read as "no pre-interest subtotal was
        reported", not as a genuine nil result: a real operating income lands
        on zero to the dollar essentially never, and repeat reads of the same
        statement showed the extractor occasionally returning 0 where it meant
        null. Taking it literally scored one co-op's EBIT margin at 0.00%
        instead of 5.90% -- four rating notches.
        """
        if self.ebit:                      # non-zero and not None
            return self.ebit
        if self.net_income is None:
            return None
        return (self.net_income + (self.interest_expense or 0.0)
                + (self.taxes or 0.0))

    @property
    def operating_cash_flow_effective(self) -> Optional[float]:
        """Reported OCF, else Net Income + Depreciation + Amortization.

        The scorecard defines OCF as net income + D&A + change in working
        capital.  When no statement of cash flows is available we can only
        reach the first two terms, so the fallback is flagged by
        :meth:`validate` rather than passed off as a reported figure.
        """
        if self.operating_cash_flow is not None:
            return self.operating_cash_flow
        if self.net_income is None:
            return None
        if self.depreciation is None and self.amortization is None:
            return None
        return (self.net_income + (self.depreciation or 0.0)
                + (self.amortization or 0.0))

    @property
    def tangible_net_worth(self) -> Optional[float]:
        """TNW = Total Equity - Intangibles - Goodwill."""
        if self.total_equity is None:
            return None
        return (self.total_equity - (self.intangible_assets or 0.0)
                - (self.goodwill or 0.0))

    @property
    def borrowed_funds(self) -> Optional[float]:
        """Borrowed Funds = LOC + current LTD + current fin leases
        + long-term debt + long-term fin leases.

        ``None`` when no debt component was determined at all -- a company
        whose debt we could not read must not be scored as debt-free.
        """
        parts = [getattr(self, name) for name in DEBT_FIELDS]
        if all(p is None for p in parts):
            return None
        return sum(p or 0.0 for p in parts)

    @property
    def annual_debt_service(self) -> Optional[float]:
        """Annual Debt Service = current LTD + current fin leases
        + Interest Expense."""
        parts = [self.current_portion_lt_debt, self.current_finance_leases,
                 self.interest_expense]
        if all(p is None for p in parts):
            return None
        return sum(p or 0.0 for p in parts)

    def total_assets(self) -> Optional[float]:
        """Total assets inferred from Liabilities + Equity."""
        if self.total_liabilities is not None and self.total_equity is not None:
            return self.total_liabilities + self.total_equity
        return None

    # -- sanity checks ------------------------------------------------------

    def validate(self) -> list[str]:
        """Return human-readable warnings about the extraction."""
        warnings: list[str] = []

        assets = self.total_assets()
        if assets is not None and assets <= 0:
            warnings.append("Liabilities + equity is zero or negative.")

        if self.total_revenue is not None and self.total_revenue <= 0:
            warnings.append("Total revenue is zero or negative.")

        if self.current_liabilities is not None and self.current_liabilities <= 0:
            warnings.append("Current liabilities are zero or negative.")

        if (self.total_liabilities is not None
                and self.current_liabilities is not None
                and self.total_liabilities + 1.0 < self.current_liabilities):
            warnings.append(
                f"Total liabilities {self.total_liabilities:,.0f} are below "
                f"current liabilities {self.current_liabilities:,.0f}."
            )

        if self.net_income is not None and self.ebitda_assumed_zero:
            warnings.append(
                "EBITDA add-backs assumed zero (not found on statement): "
                + ", ".join(a.replace("_", " ")
                            for a in self.ebitda_assumed_zero)
            )

        if (self.operating_cash_flow is None
                and self.operating_cash_flow_effective is not None):
            warnings.append(
                "No statement of cash flows found; OCF approximated as "
                "net income + depreciation + amortization."
            )

        warnings.extend(self.optimistic_gap_warnings())
        warnings.extend(self.completeness_warnings())
        warnings.extend(self.unit_scale_warnings())
        warnings.extend(self.scorecard_range_warnings())
        return warnings

    def optimistic_gap_warnings(self) -> list[str]:
        """Flag missing inputs that silently flatter the borrower.

        Borrowed Funds and Annual Debt Service add up whatever debt lines were
        read and treat the rest as zero; Tangible Net Worth deducts only the
        goodwill and intangibles it found. Every one of those gaps pushes the
        rating the same way -- towards a better score -- so they are called out
        by name rather than left for the reader to infer from a blank.
        """
        warnings: list[str] = []

        missing_debt = [f for f in DEBT_FIELDS if getattr(self, f) is None]
        if missing_debt and len(missing_debt) < len(DEBT_FIELDS):
            warnings.append(
                "Funded Debt was summed from the debt lines that were read; "
                + ", ".join(f.replace("_", " ") for f in missing_debt)
                + " could not be determined and counted as zero. Leverage is "
                  "understated and DSCR overstated by whatever they hold."
            )

        if self.total_equity is not None:
            missing_soft = [f for f in ("goodwill", "intangible_assets")
                            if getattr(self, f) is None]
            if missing_soft:
                warnings.append(
                    "Tangible net worth deducted no "
                    + " or ".join(f.replace("_", " ") for f in missing_soft)
                    + " because none was found. If the balance sheet carries "
                      "any, tangible net worth is overstated and Debt / TNW "
                      "understated."
                )
        return warnings

    def completeness_warnings(self) -> list[str]:
        """Flag a document that is not a full set of financial statements.

        Rule 3-05/3-14 carve-out exhibits ("Statements of Revenues and Direct
        Operating Expenses") carry no balance sheet or cash flow statement at
        all. Eight of the nine metrics then come back blank, which looks like a
        tool failure unless the reason is stated.
        """
        warnings: list[str] = []
        if self.statement_basis == "abbreviated":
            warnings.append(
                "The extractor judged this an ABBREVIATED presentation (e.g. a "
                "Rule 3-05/3-14 carve-out exhibit) rather than a complete set "
                "of financial statements. Balance sheet ratios from such a "
                "document omit liabilities the business actually owes and "
                "should not be scored without review."
            )
        if all(getattr(self, name) is None for name in BALANCE_SHEET_CORE):
            warnings.append(
                "No balance sheet or cash flow figures were found. This document "
                "may be an abbreviated exhibit (e.g. statements of revenues and "
                "direct operating expenses) rather than a full set of financial "
                "statements; only revenue-based metrics can be computed."
            )
        return warnings

    def unit_scale_warnings(self) -> list[str]:
        """Flag signs that the statement's units were applied twice.

        The classic failure is the extractor pre-multiplying "(in thousands)"
        figures and still tagging them ``thousands``, inflating revenue 1000x.
        Two signals: a scaled figure larger than any real company, and a
        printed figure that is an exact multiple of its own unit.
        """
        multiplier = UNIT_SCALES.get(self.unit_scale, 1.0)
        if multiplier == 1.0:
            return []
        reported = self.as_reported.get("total_revenue")
        if reported is None or self.total_revenue is None:
            return []

        if abs(self.total_revenue) >= IMPLAUSIBLE_REVENUE:
            return [
                f"Revenue scales to {self.total_revenue:,.0f}, larger than any "
                f"real company: unit_scale is '{self.unit_scale}' and revenue "
                f"was reported as {reported:,.0f}. The units were applied twice."
            ]

        # A statement printed "in thousands" shows e.g. 707,868 - not
        # 707,868,000. An exact multiple of the unit means the extractor most
        # likely multiplied the figure out and then tagged it anyway.
        if abs(reported) >= multiplier and reported % multiplier == 0:
            return [
                f"Revenue was reported as {reported:,.0f} with unit_scale "
                f"'{self.unit_scale}' - an exact multiple of the unit, so it may "
                f"already be in full dollars. Check the Extraction Audit sheet; "
                f"if so, revenue is overstated {multiplier:,.0f}x "
                f"(currently {self.total_revenue:,.0f})."
            ]
        return []

    def scorecard_range_warnings(self) -> list[str]:
        """Note revenue below the scorecard's lowest rating band.

        The template's Revenue bands run $10MM-$350MM. Its MATCH(...,-1) has no
        floor, so a smaller company is not rejected - it is pinned to the
        bottom band and rated 20 (Ca-C) on scale alone, however sound it is.
        Worth saying out loud: the scorecard was not built for that borrower.
        """
        if self.total_revenue is None or self.total_revenue >= SCORECARD_MIN_REVENUE:
            return []
        return [
            f"Revenue {self.total_revenue:,.0f} is below the scorecard's lowest "
            f"band (${SCORECARD_MIN_REVENUE:,.0f}), so the Revenue rating is "
            "pinned to 20 (Ca-C) on size alone. This scorecard is built for "
            "middle-market borrowers; the rating reflects that, not the credit."
        ]


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def normalise_unit_scale(value) -> str:
    """Map a free-form unit label onto a key of :data:`UNIT_SCALES`."""
    key = str(value or "raw").strip().lower()
    key = key.replace("in ", "").replace("'", "").replace("$", "").strip()
    if key in UNIT_SCALES:
        return key
    if "thousand" in key or key in ("000s", "000", "k"):
        return "thousands"
    if "million" in key or key in ("mm", "m"):
        return "millions"
    if "billion" in key or key in ("bn", "b"):
        return "billions"
    return "raw"


def _string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _to_float(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "")
        s = s.replace("—", "").replace("–", "").strip()
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1].strip()
        if s in ("", "-", "n/a", "N/A", "null", "None", "nan"):
            return None
        try:
            number = float(s)
        except ValueError:
            return None
        return -number if negative else number
    return None


# --------------------------------------------------------------------------
# The 9 metrics
# --------------------------------------------------------------------------

@dataclass
class MetricResult:
    name: str
    value: Optional[float]
    ok: bool
    detail: str = ""


def _safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _sum_opt(*values: Optional[float]) -> Optional[float]:
    if any(v is None for v in values):
        return None
    return sum(v or 0.0 for v in values)


def compute_metrics(fin: ExtractedFinancials) -> list[MetricResult]:
    """Compute the 9 metrics. Returns a list in the standard scorecard order."""
    metrics: list[MetricResult] = []
    ebitda = fin.ebitda
    ocf = fin.operating_cash_flow_effective

    # 1. Revenue
    rev = fin.total_revenue
    metrics.append(MetricResult(
        "Revenue", rev, rev is not None,
        "" if rev is not None else "Total revenue not found."))

    # 2. EBIT Margin
    ebit_margin = _safe_div(fin.ebit_effective, rev)
    metrics.append(MetricResult(
        "EBIT Margin", ebit_margin, ebit_margin is not None,
        "" if ebit_margin is not None
        else "Need EBIT (or net income) and revenue."))

    # 3. DSCR -- only meaningful when there is actual debt service.
    annual_ds = fin.annual_debt_service
    total_avail = _sum_opt(fin.net_income, fin.depreciation,
                           fin.interest_expense_positive)
    if annual_ds is None:
        dscr, detail = None, "Debt service components not found."
    elif annual_ds > 0:
        dscr = _safe_div(total_avail, annual_ds)
        detail = "" if dscr is not None else "Cash available for debt service not found."
    else:
        dscr, detail = None, "No debt service obligations (blanked)."
    metrics.append(MetricResult("DSCR", dscr, dscr is not None, detail))

    # 4. EBITDA / Interest Expense
    int_exp = fin.interest_expense_positive
    if int_exp is None:
        ebitda_int, detail = None, "No interest expense reported (blanked)."
    elif int_exp > 0:
        ebitda_int = _safe_div(ebitda, int_exp)
        detail = "" if ebitda_int is not None else "EBITDA not computable."
    else:
        ebitda_int, detail = None, "No interest expense to cover (blanked)."
    metrics.append(MetricResult("EBITDA / Interest Expense", ebitda_int,
                                ebitda_int is not None, detail))

    # 5. Current Ratio
    cur_ratio = _safe_div(fin.current_assets, fin.current_liabilities)
    metrics.append(MetricResult(
        "Current Ratio", cur_ratio, cur_ratio is not None,
        "" if cur_ratio is not None
        else "Need current assets and current liabilities."))

    # 6. OCF Ratio
    ocf_ratio = _safe_div(ocf, fin.current_liabilities)
    metrics.append(MetricResult(
        "OCF Ratio", ocf_ratio, ocf_ratio is not None,
        "" if ocf_ratio is not None
        else "Need operating cash flow and current liabilities."))

    # 7. Funded Debt / EBITDA
    borrowed = fin.borrowed_funds
    if borrowed is None:
        funded, detail = None, "No debt line items identified (blanked)."
    else:
        funded = _safe_div(borrowed, ebitda)
        detail = "" if funded is not None else "EBITDA not computable or zero."
    metrics.append(MetricResult("Funded Debt / EBITDA", funded,
                                funded is not None, detail))

    # 8. Debt / TNW
    tnw = fin.tangible_net_worth
    debt_tnw = _safe_div(fin.total_liabilities, tnw)
    if tnw is not None and tnw <= 0:
        detail = "Tangible net worth is zero or negative."
    elif debt_tnw is None:
        detail = "Need total liabilities and equity."
    else:
        detail = ""
    metrics.append(MetricResult("Debt / TNW", debt_tnw, debt_tnw is not None,
                                detail))

    # 9. OCF / Debt
    ocf_debt = _safe_div(ocf, fin.total_liabilities)
    metrics.append(MetricResult(
        "OCF / Debt", ocf_debt, ocf_debt is not None,
        "" if ocf_debt is not None
        else "Need operating cash flow and total liabilities."))

    return metrics


#: Scorecard columns whose rating formula reads a blank input as zero and then
#: awards the BEST rating (1 = Aaa). Verified by filling D5:L5 with blanks and
#: recalculating the template: J6 and K6 return 1, the other seven return 20
#: or 16.3. Either way a blank is scored, never left unrated, so a metric we
#: could not compute silently becomes a rating nobody checked.
BLANK_RATES_BEST: dict[str, str] = {
    "Funded Debt / EBITDA": "J",
    "Debt / TNW": "K",
}


def blank_metric_warnings(metrics: list[MetricResult]) -> list[str]:
    """Warn that the template rates blank inputs rather than skipping them."""
    blanks = [m.name for m in metrics if m.value is None or not m.ok]
    if not blanks:
        return []
    warnings = [
        f"{len(blanks)} of 9 metrics could not be computed ({', '.join(blanks)}). "
        "The template still prints an ElCap rating for a blank cell, so those "
        "ratings - and the combined score in D7 - are not meaningful."
    ]
    flattering = [f"{name} (column {BLANK_RATES_BEST[name]})"
                  for name in blanks if name in BLANK_RATES_BEST]
    if flattering:
        warnings.append(
            "Blank inputs rate as 1 (Aaa, the BEST rating) for: "
            + ", ".join(flattering)
            + ". Do not read those as strong; they are unrated."
        )
    return warnings


def render_metrics_for_excel(metrics: list[MetricResult]) -> list:
    """Convert metric results into the 9 cell values for D5:L5.

    Order: Revenue, EBIT Margin, DSCR, EBITDA/Interest, Current Ratio,
    OCF Ratio, Funded Debt/EBITDA, Debt/TNW, OCF/Debt.  ``None`` marks a
    metric that could not be computed and is written as a blank cell.
    """
    values: list = []
    for m in metrics:
        if m.value is None or not m.ok or m.value != m.value:  # NaN-safe
            values.append(None)
        else:
            rounded = (round(m.value, 6) if abs(m.value) < 1e9
                       else round(m.value, 2))
            values.append(rounded + 0.0 if rounded == 0 else rounded)  # -0.0 -> 0.0
    return values
