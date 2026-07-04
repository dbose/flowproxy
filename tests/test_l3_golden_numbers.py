"""L3 — golden-numbers integration tests (ADR-0003).

Drives the real SemanticCompiler (MetricFlow + DuckDB) against the committed
`finance_demo` project and asserts EXACT aggregate values. These prove the
`non_additive_dimension` guardrail survives the git → dbt parse → MetricFlow →
warehouse path — the WS1 thesis.

Golden numbers are hand-derived from the seed generator (see
seeds/raw_daily_balances.csv, raw_transactions.csv):

    account_balance (semi-additive, window_choice=max) by month:
        EMEA: Jan 1500, Feb 1200, Mar 2000
        AMER: Jan 4000, Feb 4500, Mar 3000
    total_transactions by month: Jan 800, Feb 650, Mar 1400
    transaction_count by month:  Jan 5,   Feb 3,   Mar 4
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("finance_demo_built")


def _rows_as_dict(result, key_cols: tuple[str, ...], value_col: str) -> dict:
    """Index result rows by a tuple of key columns → one value column."""
    col_idx = {name: i for i, name in enumerate(result.columns)}
    out = {}
    for row in result.rows:
        key = tuple(_norm(row[col_idx[k]]) for k in key_cols)
        out[key if len(key) > 1 else key[0]] = row[col_idx[value_col]]
    return out


def _norm(v):
    """Normalize a cell for keying: dates/timestamps → 'YYYY-MM' month string."""
    s = str(v)
    return s[:7] if s[:4].isdigit() and "-" in s else s


# --------------------------------------------------------------------------- #
# The headline guardrail test
# --------------------------------------------------------------------------- #
def test_semiadditive_balance_is_end_of_month_not_sum(compiler):
    """account_balance grouped by month = end-of-month balance, per region."""
    result = compiler.execute_request(
        metrics=["account_balance"],
        dimensions=["metric_time__month", "account__region"],
    )
    balances = _rows_as_dict(result, ("metric_time__month", "account__region"), "account_balance")

    expected = {
        ("2024-01", "EMEA"): 1500, ("2024-02", "EMEA"): 1200, ("2024-03", "EMEA"): 2000,
        ("2024-01", "AMER"): 4000, ("2024-02", "AMER"): 4500, ("2024-03", "AMER"): 3000,
    }
    for key, want in expected.items():
        assert float(balances[key]) == pytest.approx(want), f"{key}: got {balances[key]}, want {want}"


def test_semiadditive_differs_from_naive_sum(compiler):
    """The guardrail must MATTER: semi-additive ≠ naive sum-over-days.

    If these were equal the non_additive_dimension would be a no-op. They
    differ by ~30x (a month of daily rows), proving a BI-emitted SUM() would
    return a materially wrong balance — which the metric definition prevents.
    """
    semi = compiler.execute_request(
        metrics=["account_balance"],
        dimensions=["metric_time__month", "account__region"],
    )
    naive = compiler.execute_request(
        metrics=["naive_balance_total"],
        dimensions=["metric_time__month", "account__region"],
    )
    semi_d = _rows_as_dict(semi, ("metric_time__month", "account__region"), "account_balance")
    naive_d = _rows_as_dict(naive, ("metric_time__month", "account__region"), "naive_balance_total")

    for key in semi_d:
        assert float(naive_d[key]) > float(semi_d[key]) * 5, (
            f"{key}: naive sum {naive_d[key]} should dwarf semi-additive {semi_d[key]}"
        )
    # Jan EMEA: 30 days, mostly 1000 stepping to 1500 → 31500, vs end-of-month 1500.
    assert float(naive_d[("2024-01", "EMEA")]) == pytest.approx(31500)


def test_balance_summable_across_accounts_within_month(compiler):
    """Semi-additive over TIME, but still additive across accounts/regions.

    Grouping by month only (no region) must SUM the two regions' end-of-month
    balances: Jan = 1500 + 4000 = 5500, etc.
    """
    result = compiler.execute_request(
        metrics=["account_balance"],
        dimensions=["metric_time__month"],
    )
    by_month = _rows_as_dict(result, ("metric_time__month",), "account_balance")
    assert float(by_month["2024-01"]) == pytest.approx(5500)   # 1500 + 4000
    assert float(by_month["2024-02"]) == pytest.approx(5700)   # 1200 + 4500
    assert float(by_month["2024-03"]) == pytest.approx(5000)   # 2000 + 3000


# --------------------------------------------------------------------------- #
# Additive metrics + join-path planning
# --------------------------------------------------------------------------- #
def test_additive_transactions_by_month(compiler):
    result = compiler.execute_request(
        metrics=["total_transactions", "transaction_count"],
        dimensions=["metric_time__month"],
    )
    amt = _rows_as_dict(result, ("metric_time__month",), "total_transactions")
    cnt = _rows_as_dict(result, ("metric_time__month",), "transaction_count")
    assert {k: float(v) for k, v in amt.items()} == {
        "2024-01": 800.0, "2024-02": 650.0, "2024-03": 1400.0,
    }
    assert {k: int(v) for k, v in cnt.items()} == {
        "2024-01": 5, "2024-02": 3, "2024-03": 4,
    }


def test_join_path_region_from_other_model(compiler):
    """`region` lives on daily_balances; transactions reach it via the shared
    `account` entity. This proves cross-model join-path planning works."""
    result = compiler.execute_request(
        metrics=["total_transactions"],
        dimensions=["account__region"],
    )
    by_region = _rows_as_dict(result, ("account__region",), "total_transactions")
    # EMEA (1001): 300 + 250 + 500 = 1050 ; AMER (2002): 500 + 400 + 900 = 1800
    assert float(by_region["EMEA"]) == pytest.approx(1050)
    assert float(by_region["AMER"]) == pytest.approx(1800)


# --------------------------------------------------------------------------- #
# Filters / limits (WS2 seam — the compiler already accepts these)
# --------------------------------------------------------------------------- #
def test_where_filter_restricts_region(compiler):
    result = compiler.execute_request(
        metrics=["total_transactions"],
        dimensions=["account__region"],
        where_constraints=["{{ Dimension('account__region') }} = 'EMEA'"],
    )
    regions = {_norm(r[result.columns.index("account__region")]) for r in result.rows}
    assert regions == {"EMEA"}


def test_limit_caps_rows(compiler):
    result = compiler.execute_request(
        metrics=["account_balance"],
        dimensions=["metric_time__month", "account__region"],
        order_by=["metric_time__month"],
        limit=2,
    )
    assert result.row_count == 2


# --------------------------------------------------------------------------- #
# Guardrail error path: unreachable dimension → clear, typed error
# --------------------------------------------------------------------------- #
def test_unknown_metric_raises_typed_error(compiler):
    from engine.exceptions import UnknownFieldError

    with pytest.raises(UnknownFieldError) as exc:
        compiler.execute_request(metrics=["does_not_exist"], dimensions=[])
    assert exc.value.sqlstate == "42703"
