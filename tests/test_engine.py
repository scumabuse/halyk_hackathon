"""Тесты расчётного ядра: знак, округление, границы порога, пустые статьи."""
from decimal import Decimal

import pytest

from src.covenants import ADD, CONST, DIV, MAX, S, SUB, Covenant
from src.engine import Context, compute, evaluate, r2


def ctx(**sums) -> Context:
    c = Context()
    c.sums = {k: Decimal(str(v)) for k, v in sums.items()}
    return c


def cov(expr, direction="max", threshold="1.00", kind="test", condition=None):
    return Covenant(clause="6.1", kind=kind, expr=expr, direction=direction,
                    threshold=Decimal(threshold), quote="", source_doc="", condition=condition)


def test_round_half_up():
    assert r2(Decimal("0.455")) == Decimal("0.46")
    assert r2(Decimal("0.454")) == Decimal("0.45")
    assert r2(Decimal("1.005")) == Decimal("1.01")


def test_actual_is_positive_for_expenses():
    """Расходы в леджере отрицательные, но actual обязан быть положительным."""
    from src.ledger import Txn
    t = Txn(txn_id="TXN-X-1", scenario="X", date="2025-01-01", account="ACC-1",
            counterparty="C", description="d", amount=Decimal("-1000.50"),
            currency="USD", is_plot=True, category="capex")
    t.amount_usd = t.amount
    assert t.magnitude == Decimal("1000.50")


def test_capital_intensity_ratio():
    c = ctx(capex=1842006.44, opex=3104882.61, rent=918443.27)
    r = compute(cov(DIV(S("capex"), ADD(S("opex"), S("rent"))), "max", "0.42"), c, "P1")
    assert r.actual == Decimal("0.46")
    assert r.status == "BREACH"


def test_threshold_boundary_is_not_a_breach():
    """«не превышал X» — равенство порогу является соблюдением."""
    c = ctx(a=100, b=1000)
    r = compute(cov(DIV(S("a"), S("b")), "max", "0.10"), c, "T")
    assert r.status == "COMPLIANT"


def test_min_direction_boundary():
    c = ctx(revenue=7100000)
    assert compute(cov(S("revenue"), "min", "7100000.00"), c, "T").status == "COMPLIANT"
    c2 = ctx(revenue=7099999.99)
    assert compute(cov(S("revenue"), "min", "7100000.00"), c2, "T").status == "BREACH"


def test_empty_category_is_zero_not_crash():
    r = compute(cov(S("related_party"), "max", "450000.00"), ctx(), "T")
    assert r.actual == Decimal("0.00") and r.status == "COMPLIANT"


def test_division_by_zero_yields_no_value():
    r = compute(cov(DIV(S("a"), S("b")), "max", "1.00"), ctx(a=5), "T")
    assert r.actual is None


def test_max_of_two_overheads():
    """Individual Overhead Line Ceiling: проверяется наибольшая статья, не сумма."""
    c = ctx(payroll=1284663.42, utilities=937215.88)
    r = compute(cov(MAX(S("payroll"), S("utilities")), "max", "1500000.00"), c, "B1")
    assert r.actual == Decimal("1284663.42") and r.status == "COMPLIANT"


def test_springing_not_triggered_still_reports_real_metric():
    """Если триггер не сработал — статус COMPLIANT, но actual остаётся фактическим."""
    c = ctx(financing=1000000, revenue=5000000, opex=4000000)
    spec = cov(DIV(S("financing"), SUB(S("revenue"), S("opex"))), "max", "1.70",
               condition={"expr": S("financing"), "op": ">", "value": Decimal("4000000")})
    r = compute(spec, c, "P3")
    assert r.status == "COMPLIANT"
    assert r.actual == Decimal("1.00")      # не null
    assert r.applied is False


def test_springing_triggered_breaches():
    c = ctx(financing=5442118.93, revenue=8104772.36, opex=4928952.24)
    spec = cov(DIV(S("financing"), SUB(S("revenue"), S("opex"))), "max", "1.70",
               condition={"expr": S("financing"), "op": ">", "value": Decimal("4000000")})
    r = compute(spec, c, "P3")
    assert r.actual == Decimal("1.71") and r.status == "BREACH"


def test_missing_disclosed_constant_is_not_silently_zero():
    c = ctx(payroll=1000)
    r = compute(cov(ADD(S("payroll"), CONST("severance")), "max", "4000000.00"), c, "P8")
    assert r.actual is None            # ноль подставлять нельзя — это была бы тихая ошибка
    assert "severance" in c.missing_consts


def test_disclosed_constant_is_added():
    c = ctx(payroll=3302867.43)
    c.consts["severance"] = Decimal("918447.52")
    r = compute(cov(ADD(S("payroll"), CONST("severance")), "max", "4000000.00"), c, "P8")
    assert r.actual == Decimal("4221314.95") and r.status == "BREACH"


def test_quarter_filter():
    c = Context()
    c.quarter_sums = {("revenue", 4): Decimal("3084375.68"), ("revenue", 1): Decimal("2048994.35")}
    r = compute(cov(S("revenue", quarter=4), "min", "3500000.00"), c, "B4")
    assert r.actual == Decimal("3084375.68") and r.status == "BREACH"
