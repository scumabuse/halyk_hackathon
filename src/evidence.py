"""Выбор решающей транзакции (evidence_txn_id).

Условие задачи определяет доказательство узко: это операция, чья ПЕРЕКЛАССИФИКАЦИЯ,
ВКЛЮЧЕНИЕ, ИСКЛЮЧЕНИЕ или ИСПРАВЛЕНИЕ приводит к нарушению — уберите её, и вердикт
изменится. Операция, которая лишь вносит вклад в сумму (самая крупная строка, последняя
перед закрытием периода, та, что случайно вывела накопленную сумму за порог),
доказательством НЕ является.

Отсюда два шага:
  1. кандидатами становятся только «особые» операции — затронутые корректировкой
     аудитора, сопоставленные со связанной стороной или включённые в расчёт из-за
     признака из документа (например, передача активов неограниченной дочерней
     организации). Обычные строки статьи в кандидаты не попадают, иначе у агрегатного
     лимита «решающей» окажется любая из них;
  2. для каждого кандидата показатель пересчитывается БЕЗ него; доказательством
     становится тот, на котором вердикт переворачивается.

Если ковенант соблюдён, доказывать нечего — возвращается null.
"""
from __future__ import annotations

from decimal import Decimal

from .engine import build_context, compute
from .logger import log


def candidates(txns, adjusted_ids: set[str]) -> list:
    out = []
    for t in txns:
        if not t.is_plot or t.amount_usd is None:
            continue
        if t.txn_id in adjusted_ids or t.category in ("related_party", "capex_transfer"):
            out.append(t)
    return out


def select(cov, result, txns, adjusted_ids, disclosed, addbacks, unrestricted, scenario):
    """Возвращает (txn_id | None, журнал проверок)."""
    log_lines: list[str] = []
    if result.status != "BREACH" or result.actual is None:
        return None, ["ковенант соблюдён — решающей операции нет"]

    found = None
    for t in candidates(txns, adjusted_ids):
        subset = [x for x in txns if x.txn_id != t.txn_id]
        ctx2 = build_context(subset, disclosed, addbacks, unrestricted, scenario)
        r2 = compute(cov, ctx2, scenario)
        flipped = r2.actual is not None and r2.status != result.status
        log_lines.append(
            f"без {t.txn_id} ({t.category}, {t.magnitude:,.2f}): "
            f"{r2.actual if r2.actual is not None else '—'} → {r2.status}"
            f"{'  ⇒ ВЕРДИКТ МЕНЯЕТСЯ' if flipped else ''}"
        )
        if flipped and found is None:
            found = t.txn_id

    if found:
        log.info("%s п.%s: решающая операция — %s", scenario, cov.clause, found)
    else:
        log_lines.append("ни одна отдельная операция не переворачивает вердикт — доказательство null")
    return found, log_lines
