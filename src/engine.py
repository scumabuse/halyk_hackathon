"""Детерминированный расчёт показателей ковенантов.

Арифметику никогда не выполняет модель — только Decimal и правила ниже:
  * actual ВСЕГДА положительный: расходы в леджере отрицательные, берётся модуль;
  * округление ROUND_HALF_UP до 2 знаков — как требует условие задачи;
  * сравнение с порогом идёт по НЕокруглённому значению (округление только для вывода);
  * условный (springing) ковенант: если триггер не сработал — статус COMPLIANT,
    но actual всё равно равен фактическому значению показателя, а не null.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from .logger import log, loud

CENT = Decimal("0.01")


def r2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Context:
    """Суммы по статьям (уже положительные) плюс величины, раскрытые в документах."""
    sums: dict[str, Decimal] = field(default_factory=dict)
    quarter_sums: dict[tuple[str, int], Decimal] = field(default_factory=dict)
    consts: dict[str, Decimal] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)   # статья → txn_id
    missing_consts: list[str] = field(default_factory=list)

    def total(self, cats: list[str], quarter: int | None) -> Decimal:
        if quarter is None:
            return sum((self.sums.get(c, Decimal(0)) for c in cats), Decimal(0))
        return sum((self.quarter_sums.get((c, quarter), Decimal(0)) for c in cats), Decimal(0))


def build_context(txns, disclosed: dict[str, Decimal], addbacks: Decimal | None,
                  unrestricted_names: set[str], scenario: str) -> Context:
    ctx = Context()
    for t in txns:
        if not t.is_plot or t.category in (None, "other", "excluded_period"):
            continue
        if t.amount_usd is None:
            continue
        cat = t.category
        val = t.magnitude
        ctx.sums[cat] = ctx.sums.get(cat, Decimal(0)) + val
        ctx.members.setdefault(cat, []).append(t.txn_id)
        q = t.quarter
        if q:
            ctx.quarter_sums[(cat, q)] = ctx.quarter_sums.get((cat, q), Decimal(0)) + val

        # Передача активов дочерней организации вне периметра обеспечения — отдельная статья.
        if cat == "capex_transfer" and _is_unrestricted(t.counterparty, unrestricted_names):
            ctx.sums["capex_transfer_unrestricted"] = (
                ctx.sums.get("capex_transfer_unrestricted", Decimal(0)) + val)
            ctx.members.setdefault("capex_transfer_unrestricted", []).append(t.txn_id)

    for k, v in disclosed.items():
        ctx.consts[k] = v
    if addbacks is not None:
        ctx.consts["addbacks"] = addbacks

    log.info("%s: суммы по статьям — %s", scenario,
             ", ".join(f"{k}={v:,.2f}" for k, v in sorted(ctx.sums.items())) or "пусто")
    return ctx


def _is_unrestricted(counterparty: str, names: set[str]) -> bool:
    import re
    cp = re.sub(r"[^a-z ]+", " ", (counterparty or "").lower())
    return any(n and n in cp for n in names)


def evaluate(expr: dict, ctx: Context, trace: list[str]) -> Decimal | None:
    """Считает дерево выражения; None означает «данных нет» (а не ноль)."""
    if "agg" in expr:
        cats, q = expr["cats"], expr.get("quarter")
        val = ctx.total(cats, q)
        label = "+".join(cats) + (f" (кв.{q})" if q else "")
        ids = ",".join(sum((ctx.members.get(c, []) for c in cats), []))
        trace.append(f"{label} = {val:,.2f}" + (f" [{ids}]" if ids else " [нет операций]"))
        return val
    if "const" in expr:
        key = expr["const"]
        if key not in ctx.consts:
            ctx.missing_consts.append(key)
            trace.append(f"{key} = НЕ РАСКРЫТО в документах")
            return None
        trace.append(f"{key} = {ctx.consts[key]:,.2f} (раскрыто в документе)")
        return ctx.consts[key]

    op = expr["op"]
    if op in ("div", "sub"):
        l = evaluate(expr["l"], ctx, trace)
        r = evaluate(expr["r"], ctx, trace)
        if l is None or r is None:
            return None
        if op == "sub":
            return l - r
        if r == 0:
            trace.append("делитель равен нулю — показатель не определён")
            return None
        return l / r
    vals = [evaluate(a, ctx, trace) for a in expr["args"]]
    if any(v is None for v in vals):
        return None
    return sum(vals, Decimal(0)) if op == "add" else max(vals)


@dataclass
class Result:
    actual: Decimal | None
    status: str
    trace: str
    applied: bool = True


def compute(cov, ctx: Context, scenario: str) -> Result:
    trace: list[str] = []
    value = evaluate(cov.expr, ctx, trace)

    triggered, cond_note = True, ""
    if cov.condition:
        ctrace: list[str] = []
        cval = evaluate(cov.condition["expr"], ctx, ctrace)
        triggered = cval is not None and cval > cov.condition["value"]
        cond_note = (f"условие применения: {' ; '.join(ctrace)} "
                     f"{'>' if triggered else '≤'} {cov.condition['value']:,.2f} → "
                     f"{'СРАБОТАЛО' if triggered else 'не сработало'}")

    if value is None:
        loud(f"{scenario} п.{cov.clause}: показатель не вычислен — "
             f"не хватает данных ({', '.join(ctx.missing_consts) or 'нет операций в статьях'}). "
             "Значение в сабмите будет приблизительным.")
        return Result(None, "COMPLIANT", " ; ".join(trace + ([cond_note] if cond_note else [])))

    if not triggered:
        status = "COMPLIANT"        # ковенант не применяется, но actual — реальное значение
    elif cov.direction == "max":
        status = "BREACH" if value > cov.threshold else "COMPLIANT"
    else:
        status = "BREACH" if value < cov.threshold else "COMPLIANT"

    sign = "≤" if cov.direction == "max" else "≥"
    line = (f"{' ; '.join(trace)} ⇒ {r2(value)} против порога {sign} {cov.threshold} ⇒ {status}")
    if cond_note:
        line = cond_note + " ; " + line
    return Result(r2(value), status, line, applied=triggered)
