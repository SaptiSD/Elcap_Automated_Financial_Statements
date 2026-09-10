"""Metric expectations, hand-computed from primary sources.

Two anchor fixtures:

* ``ELCAP_DEMO`` reproduces the worked example that ships inside the ElCap
  workbook itself ("Scorecard to ElCap" row 5), where a human wrote the
  arithmetic straight into the cells -- e.g. F5 is ``=(-35+141+55)/(144+55)``.
  Those formulas are the closest thing we have to an authored answer key, so
  the engine is checked against them directly.
* ``ZENCODER_FY2011`` is transcribed by hand from
  ``sample/input/Audited Financial Statements.pdf`` (Zencoder Inc., year ended
  December 31 2011).
"""

import math

import pytest

from compute_metrics import (ExtractedFinancials, compute_metrics,
                             normalise_unit_scale, render_metrics_for_excel)

METRIC_ORDER = [
    "Revenue", "EBIT Margin", "DSCR", "EBITDA / Interest Expense",
    "Current Ratio", "OCF Ratio", "Funded Debt / EBITDA", "Debt / TNW",
    "OCF / Debt",
]


def values_by_name(fin):
    return {m.name: m.value for m in compute_metrics(fin)}


# --------------------------------------------------------------------------
# The worked example inside the ElCap workbook (figures printed in thousands)
# --------------------------------------------------------------------------

ELCAP_DEMO = {
    "company_name": "ElCap worked example",
    "fiscal_year": "2025",
    "unit_scale": "thousands",
    "total_revenue": 2843,
    "ebit": -193,
    "net_income": -35,
    "depreciation": 141,
    "interest_expense": 55,
    "current_portion_lt_debt": 144,
    "long_term_debt": 300.36,
    "operating_cash_flow": 72,          # = -35 + 141 - 34 per the sheet
    "current_assets": 325.38,           # gives the sheet's 0.51 current ratio
    "current_liabilities": 638,
    "total_liabilities": 2454,
    "total_equity": -1704.1666666666667,  # gives the sheet's -1.44 Debt/TNW
    "absent_line_items": ["taxes", "amortization", "goodwill",
                          "intangible_assets", "line_of_credit",
                          "current_finance_leases", "long_term_finance_leases"],
}


@pytest.fixture
def elcap_demo():
    return ExtractedFinancials.from_dict(ELCAP_DEMO)


def test_demo_units_are_scaled_to_dollars(elcap_demo):
    # D5 in the shipped workbook is 2,843,000 -- dollars, not thousands.
    assert elcap_demo.total_revenue == 2_843_000
    assert elcap_demo.current_liabilities == 638_000


def test_demo_absent_items_become_zero(elcap_demo):
    assert elcap_demo.taxes == 0.0
    assert elcap_demo.amortization == 0.0
    assert elcap_demo.line_of_credit == 0.0


def test_demo_ebitda_matches_the_sheet(elcap_demo):
    # G5 is =161/55, so the author's EBITDA was 161 (thousands).
    assert elcap_demo.ebitda == pytest.approx(161_000)


@pytest.mark.parametrize("metric,expected", [
    ("Revenue", 2_843_000),
    ("EBIT Margin", -193 / 2843),                  # E5 =(-193)/2843
    ("DSCR", (-35 + 141 + 55) / (144 + 55)),       # F5
    ("EBITDA / Interest Expense", 161 / 55),       # G5 =161/55
    ("Current Ratio", 0.51),                       # H5
    ("OCF Ratio", (-35 + 141 - 34) / 638),         # I5
    ("Funded Debt / EBITDA", 2.76),                # J5
    ("Debt / TNW", -1.44),                         # K5
    ("OCF / Debt", (-35 + 141 - 34) / 2454),       # L5
])
def test_demo_metrics_match_the_authored_workbook(elcap_demo, metric, expected):
    assert values_by_name(elcap_demo)[metric] == pytest.approx(expected, rel=1e-4)


# --------------------------------------------------------------------------
# Zencoder Inc., FY2011 -- transcribed from the sample audited statements
# --------------------------------------------------------------------------

ZENCODER_FY2011 = {
    "company_name": "Zencoder Inc.",
    "fiscal_year": "2011",
    "unit_scale": "raw",
    "total_revenue": 707868,
    "ebit": -1533611,           # Loss from operations
    "net_income": -1484458,     # Net loss
    "interest_expense": -49466,  # Interest income (expense), net -> income
    "depreciation": 12520,
    "current_assets": 888160,
    "current_liabilities": 248149,
    "total_liabilities": 248149,
    "total_equity": 690214,
    "operating_cash_flow": -1384052,
    "absent_line_items": ["taxes", "amortization", "goodwill",
                          "intangible_assets", "line_of_credit",
                          "current_portion_lt_debt", "current_finance_leases",
                          "long_term_debt", "long_term_finance_leases"],
}


@pytest.fixture
def zencoder():
    return ExtractedFinancials.from_dict(ZENCODER_FY2011)


def test_zencoder_balance_sheet_ties(zencoder):
    # Total assets per the statement are 938,363.
    assert zencoder.total_assets() == pytest.approx(938_363)


def test_zencoder_computable_metrics(zencoder):
    values = values_by_name(zencoder)
    assert values["Revenue"] == 707868
    assert values["EBIT Margin"] == pytest.approx(-1533611 / 707868)
    assert values["Current Ratio"] == pytest.approx(888160 / 248149)
    assert values["OCF Ratio"] == pytest.approx(-1384052 / 248149)
    assert values["Debt / TNW"] == pytest.approx(248149 / 690214)
    assert values["OCF / Debt"] == pytest.approx(-1384052 / 248149)


def test_zencoder_coverage_metrics_are_blank(zencoder):
    """Zencoder had no borrowings and net interest income, so the three
    debt-coverage metrics are not applicable rather than zero."""
    values = values_by_name(zencoder)
    assert values["DSCR"] is None
    assert values["EBITDA / Interest Expense"] is None


def test_zencoder_debt_free_scores_zero_leverage(zencoder):
    """The statement affirmatively shows no borrowings, so Funded Debt is 0."""
    assert zencoder.borrowed_funds == 0.0
    assert values_by_name(zencoder)["Funded Debt / EBITDA"] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Unit handling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("thousands", 1_000.0), ("in thousands", 1_000.0), ("000s", 1_000.0),
    ("millions", 1_000_000.0), ("MM", 1_000_000.0),
    ("billions", 1_000_000_000.0),
    ("raw", 1.0), ("dollars", 1.0), (None, 1.0), ("gibberish", 1.0),
])
def test_unit_scale_multiplier(label, expected):
    from compute_metrics import UNIT_SCALES
    assert UNIT_SCALES[normalise_unit_scale(label)] == expected


def test_millions_are_scaled():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "millions", "total_revenue": 67589})
    assert fin.total_revenue == 67_589_000_000


def test_ratios_are_unit_invariant():
    """Scaling changes Revenue but must not move any ratio."""
    base = dict(ELCAP_DEMO)
    as_dollars = {k: (v * 1000 if isinstance(v, (int, float)) else v)
                  for k, v in base.items()}
    as_dollars["unit_scale"] = "raw"
    thousands = values_by_name(ExtractedFinancials.from_dict(base))
    dollars = values_by_name(ExtractedFinancials.from_dict(as_dollars))
    for name in METRIC_ORDER[1:]:
        assert thousands[name] == pytest.approx(dollars[name])


def test_absurd_scaled_revenue_is_flagged():
    """A 'millions' tag on an already-multiplied figure is unmissable."""
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "millions", "total_revenue": 67_589_000_000})
    assert "applied twice" in fin.unit_scale_warnings()[0]


def test_premultiplied_thousands_are_flagged():
    """707,868,000 printed under an '(in thousands)' heading is a tell."""
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "thousands", "total_revenue": 707_868_000})
    assert "already be in full dollars" in fin.unit_scale_warnings()[0]


def test_large_but_real_revenue_is_not_flagged():
    """$3.83bn reported in thousands is ordinary for a big filer."""
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "thousands", "total_revenue": 3_830_200})
    assert fin.total_revenue == 3_830_200_000
    assert fin.unit_scale_warnings() == []


def test_revenue_below_the_scorecard_band_is_flagged():
    """Under $10MM the template pins Revenue to the bottom band, rating 20 --
    confirmed by recalculating the shipped workbook, whose $2.843MM revenue
    rates 20 rather than erroring."""
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "total_revenue": 707_868})
    assert "pinned to 20" in fin.scorecard_range_warnings()[0]
    assert ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "total_revenue": 50_000_000}
    ).scorecard_range_warnings() == []


# --------------------------------------------------------------------------
# Absent vs. unknown
# --------------------------------------------------------------------------

def test_unknown_debt_leaves_leverage_blank():
    """Nulls mean 'not read', which must not be scored as debt-free."""
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "net_income": 100, "total_revenue": 1000})
    assert fin.borrowed_funds is None
    assert values_by_name(fin)["Funded Debt / EBITDA"] is None


def test_absent_debt_scores_as_zero():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "net_income": 100, "total_revenue": 1000,
        "absent_line_items": ["line_of_credit", "current_portion_lt_debt",
                              "current_finance_leases", "long_term_debt",
                              "long_term_finance_leases"],
    })
    assert fin.borrowed_funds == 0.0
    assert values_by_name(fin)["Funded Debt / EBITDA"] == 0.0


def test_missing_addbacks_do_not_destroy_ebitda():
    """A statement with no tax or amortization line still has an EBITDA."""
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "net_income": 100, "interest_expense": 10,
        "depreciation": 20,
    })
    assert fin.ebitda == pytest.approx(130)
    assert set(fin.ebitda_assumed_zero) == {"taxes", "amortization"}
    assert any("assumed zero" in w for w in fin.validate())


def test_ocf_falls_back_to_net_income_plus_da():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "net_income": -100, "depreciation": 30,
        "current_liabilities": 200,
    })
    assert fin.operating_cash_flow_effective == pytest.approx(-70)
    assert any("approximated" in w for w in fin.validate())


def test_ocf_fallback_needs_da():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "net_income": -100, "current_liabilities": 200})
    assert fin.operating_cash_flow_effective is None


# --------------------------------------------------------------------------
# Individual metric edge cases
# --------------------------------------------------------------------------

def test_interest_income_is_not_coverage():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "net_income": 100, "interest_expense": -50})
    assert fin.interest_expense_positive is None
    assert values_by_name(fin)["EBITDA / Interest Expense"] is None


def test_ebit_falls_back_to_net_income_plus_interest_and_tax():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "total_revenue": 1000, "net_income": 50,
        "interest_expense": 20, "taxes": 30,
    })
    assert fin.ebit_effective == pytest.approx(100)
    assert values_by_name(fin)["EBIT Margin"] == pytest.approx(0.1)


def test_reported_ebit_wins_over_the_fallback():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "total_revenue": 1000, "net_income": 50,
        "interest_expense": 20, "taxes": 30, "ebit": 90,
    })
    assert values_by_name(fin)["EBIT Margin"] == pytest.approx(0.09)


def test_tangible_net_worth_strips_goodwill_and_intangibles():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "total_equity": 1000, "goodwill": 300,
        "intangible_assets": 200, "total_liabilities": 500,
    })
    assert fin.tangible_net_worth == pytest.approx(500)
    assert values_by_name(fin)["Debt / TNW"] == pytest.approx(1.0)


def test_zero_tangible_net_worth_is_blank_not_infinite():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "total_equity": 300, "goodwill": 300,
        "total_liabilities": 500,
    })
    assert values_by_name(fin)["Debt / TNW"] is None


def test_no_debt_service_blanks_dscr():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "net_income": 100, "depreciation": 10,
        "absent_line_items": ["current_portion_lt_debt",
                              "current_finance_leases", "interest_expense"],
    })
    assert values_by_name(fin)["DSCR"] is None


def test_dscr_uses_the_workbook_formula():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "net_income": 200, "depreciation": 100,
        "interest_expense": 50, "current_portion_lt_debt": 100,
        "current_finance_leases": 25,
    })
    assert values_by_name(fin)["DSCR"] == pytest.approx(350 / 175)


# --------------------------------------------------------------------------
# Number parsing and Excel rendering
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1,234", 1234.0), ("$1,234", 1234.0), ("(1,484,458)", -1484458.0),
    ("-35", -35.0), ("", None), ("—", None), ("n/a", None), (None, None),
    (True, None), (12, 12.0),
])
def test_number_parsing(raw, expected):
    from compute_metrics import _to_float
    assert _to_float(raw) == expected


def test_render_produces_nine_cells_with_blanks():
    fin = ExtractedFinancials.from_dict(ZENCODER_FY2011)
    values = render_metrics_for_excel(compute_metrics(fin))
    assert len(values) == 9
    assert values[2] is None and values[3] is None      # DSCR, EBITDA/Int
    assert values[0] == 707868


def test_render_blanks_nan():
    from compute_metrics import MetricResult
    values = render_metrics_for_excel(
        [MetricResult(n, math.nan, True) for n in METRIC_ORDER])
    assert values == [None] * 9


# --------------------------------------------------------------------------
# Incomplete documents
# --------------------------------------------------------------------------

def test_abbreviated_exhibit_is_called_out():
    """Rule 3-05 carve-out exhibits carry revenue and nothing else."""
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "total_revenue": 18_682_920})
    assert "abbreviated exhibit" in fin.completeness_warnings()[0]


def test_complete_statements_are_not_flagged(zencoder):
    assert zencoder.completeness_warnings() == []


def test_one_missing_core_item_is_not_enough_to_flag():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "total_revenue": 1000, "current_assets": 500})
    assert fin.completeness_warnings() == []


def test_abbreviated_basis_is_flagged_even_with_a_full_balance_sheet():
    """A carve-out 'assets acquired and liabilities assumed' statement has
    every line item and still must not be scored as a going concern."""
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "millions", "statement_basis": "abbreviated",
        "total_revenue": 1454, "current_assets": 152.9,
        "current_liabilities": 0.3, "total_liabilities": 0.5,
        "total_equity": 683.4, "operating_cash_flow": 10,
    })
    assert "ABBREVIATED" in fin.completeness_warnings()[0]


def test_complete_basis_is_quiet(zencoder):
    assert ExtractedFinancials.from_dict(
        {**ZENCODER_FY2011, "statement_basis": "complete"}
    ).completeness_warnings() == []


def test_unrecognised_basis_falls_back_to_unknown():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "statement_basis": "sort of complete?"})
    assert fin.statement_basis == "unknown"


# --------------------------------------------------------------------------
# Blank inputs are still rated by the template
# --------------------------------------------------------------------------

def test_no_blank_warning_when_everything_computes(elcap_demo):
    from compute_metrics import blank_metric_warnings
    assert blank_metric_warnings(compute_metrics(elcap_demo)) == []


def test_blank_metrics_are_named(zencoder):
    from compute_metrics import blank_metric_warnings
    warnings = blank_metric_warnings(compute_metrics(zencoder))
    assert "DSCR" in warnings[0] and "not meaningful" in warnings[0]


def test_leverage_blanks_are_called_out_as_flattering():
    """A blank J5/K5 rates Aaa in the template, so it must not read as strong."""
    from compute_metrics import MetricResult, blank_metric_warnings
    metrics = [MetricResult("Debt / TNW", None, False)]
    warnings = blank_metric_warnings(metrics)
    assert "BEST rating" in warnings[1] and "column K" in warnings[1]


# --------------------------------------------------------------------------
# Gaps that flatter the borrower
# --------------------------------------------------------------------------

def test_zero_ebit_is_treated_as_not_reported():
    """Repeat reads showed the extractor sometimes returning 0 where it meant
    null; taken literally that scored a 5.90% EBIT margin as 0.00%."""
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "ebit": 0, "total_revenue": 130_264_909,
        "net_income": 5_755_614, "interest_expense": 1_930_153, "taxes": 0,
    })
    assert fin.ebit_effective == pytest.approx(7_685_767)
    assert values_by_name(fin)["EBIT Margin"] == pytest.approx(0.0590, abs=1e-4)


def test_a_real_negative_ebit_is_still_used():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "ebit": -1_533_611, "total_revenue": 707_868,
         "net_income": -1_484_458})
    assert fin.ebit_effective == -1_533_611


def test_partially_read_debt_is_called_out():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "line_of_credit": 7_508,
        "current_portion_lt_debt": 23_844,     # long-term debt not found
    })
    warning = fin.optimistic_gap_warnings()[0]
    assert "long term debt" in warning and "understated" in warning


def test_fully_known_debt_is_not_flagged():
    fin = ExtractedFinancials.from_dict({
        "unit_scale": "raw", "line_of_credit": 1, "current_portion_lt_debt": 1,
        "current_finance_leases": 1, "long_term_debt": 1,
        "long_term_finance_leases": 1, "goodwill": 0, "intangible_assets": 0,
        "total_equity": 100,
    })
    assert fin.optimistic_gap_warnings() == []


def test_entirely_unknown_debt_is_not_double_reported():
    """All-unknown is already handled by blanking the metric outright."""
    fin = ExtractedFinancials.from_dict({"unit_scale": "raw", "net_income": 1})
    assert not any("Funded Debt was summed" in w
                   for w in fin.optimistic_gap_warnings())


def test_unfound_intangibles_are_called_out():
    fin = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "total_equity": 1000, "total_liabilities": 500})
    assert any("tangible net worth is overstated" in w
               for w in fin.optimistic_gap_warnings())
