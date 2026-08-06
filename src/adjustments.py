"""Корректировки аудитора и данные, которых нет в леджере.

Аудиторские документы делают шесть разных вещей, и путать их нельзя:

  1. ПЕРЕКЛАССИФИКАЦИЯ — сумма переносится в другую статью. Адресуется либо по
     txn_id, либо по паре «сумма + контрагент» (в части документов txn_id не назван).
  2. ОТКЛОНЁННАЯ переклассификация — «рассматривалась … первоначальная классификация
     сохраняется». Её применять НЕЛЬЗЯ; договоры прямо оговаривают, что рассмотренные
     и отклонённые реклассификации в расчёт не принимаются.
  3. ОТСЕЧЕНИЕ ПЕРИОДА — операция исключается из ковенантного периода (услуги/переход
     рисков приходятся на следующий год).
  4. ВОССТАНОВЛЕНИЕ СУММЫ — строка есть, а суммы в выгрузке нет; фактическая сумма
     раскрыта в примечании или в записке казначейства.
  5. РАСКРЫТОЕ ОБЯЗАТЕЛЬСТВО — величина, которой вообще нет отдельной проводкой
     (например, обязательство по программе выходных пособий).
  6. КУРС ВАЛЮТЫ — отдельной таблицы курсов нет, курс выводится из пары
     «сумма в EUR ↔ фактический платёж в USD».
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .logger import log, loud

MONEY = r"\$?([0-9][0-9\s,]*\.\d{2})"
RE_TXN = re.compile(r"(TXN-[A-Za-z0-9]+-\d+)")

CATEGORY_WORDS = {
    "операционные расходы": "opex",
    "страховые премии": "insurance",
    "процентные расходы": "interest",
    "капитальные затраты": "capex",
    "выручка": "revenue",
    "расходы на оплату труда": "payroll",
    "коммунальные расходы": "utilities",
    "арендные платежи": "rent",
}


def money(s: str) -> Decimal:
    return Decimal(re.sub(r"[\s,]", "", s))


@dataclass
class Adjustment:
    kind: str                       # reclass | exclude | set_amount | rejected
    txn_id: str | None = None
    amount: Decimal | None = None
    counterparty: str | None = None
    to_category: str | None = None
    from_category: str | None = None
    quote: str = ""
    source_doc: str = ""


@dataclass
class ScenarioFacts:
    adjustments: list[Adjustment] = field(default_factory=list)
    disclosed: dict[str, Decimal] = field(default_factory=dict)   # раскрытые обязательства
    fx_rates: dict[str, Decimal] = field(default_factory=dict)
    one_offs: list[tuple[str, Decimal]] = field(default_factory=list)
    one_off_floor: Decimal | None = None


def _sentences(text: str) -> list[str]:
    """Примечания идут пунктами «(7.1) …» — режем по ним, иначе по абзацам."""
    flat = re.sub(r"\s*\n\s*", " ", text)
    parts = re.split(r"(?=\(\d+\.\d+\))", flat)
    return [p.strip() for p in parts if p.strip()]


def _category_after(text: str, marker: str) -> str | None:
    m = re.search(marker + r"[^.]{0,80}?как\s+([А-Яа-яЁё ]+)", text)
    if not m:
        return None
    return CATEGORY_WORDS.get(m.group(1).strip().lower().rstrip(" ."))


def parse_audit(text: str, doc_name: str, scenario: str, facts: ScenarioFacts) -> None:
    """Разбирает один аудиторский документ и дополняет факты по сценарию."""
    if "Переклассификаций за ковенантный период не требовалось" in text:
        log.info("%s: аудитор прямо указал, что переклассификаций не требовалось (%s)", scenario, doc_name)

    for s in _sentences(text):
        low = s.lower()
        txn = RE_TXN.search(s)
        amounts = re.findall(MONEY, s)

        # 2. Отклонённая переклассификация — фиксируем и НЕ применяем.
        if "сохраняется" in low and ("не производилась" in low or "не требуется" in low or "отклонен" in low):
            facts.adjustments.append(Adjustment(
                kind="rejected", txn_id=txn.group(1) if txn else None,
                amount=money(amounts[0]) if amounts else None,
                quote=s[:280], source_doc=doc_name))
            log.info("%s: реклассификация РАССМОТРЕНА И ОТКЛОНЕНА — не применяем (%s)",
                     scenario, (txn.group(1) if txn else s[:60]))
            continue

        # 4. Сумма отсутствует в выгрузке.
        if "не отражена в выгрузке" in low and amounts and txn:
            facts.adjustments.append(Adjustment(
                kind="set_amount", txn_id=txn.group(1), amount=money(amounts[-1]),
                quote=s[:280], source_doc=doc_name))
            log.info("%s: сумма операции %s восстановлена из документа: %s",
                     scenario, txn.group(1), amounts[-1])
            continue

        # 5. Раскрытое обязательство без отдельной проводки.
        if "раскрывается и не отражается отдельной операцией" in low and amounts:
            key = "severance" if "выходн" in low else "disclosed"
            facts.disclosed[key] = money(amounts[0])
            log.info("%s: раскрыто обязательство «%s» на сумму %s (отдельной проводки нет)",
                     scenario, key, amounts[0])
            continue

        # 6. Курс валюты выводится из пары «счёт в EUR ↔ платёж в USD».
        m = re.search(r"([0-9][0-9\s,]*\.\d{2})\s*(EUR|EURO|евро)[^.]{0,90}?" + MONEY, s, re.I)
        if m:
            eur, usd = money(m.group(1)), money(m.group(3))
            if eur > 0:
                rate = (usd / eur).quantize(Decimal("0.000001"))
                facts.fx_rates["EUR"] = rate
                log.info("%s: курс EUR→USD выведен из фактического расчёта: %s/%s = %s",
                         scenario, usd, eur, rate)
            continue

        # 3. Отсечение периода.
        if txn and ("исключена из ковенантного периода" in low
                    or re.search(r"относится к услугам, оказанным в период с (\d{4})", low)):
            facts.adjustments.append(Adjustment(
                kind="exclude", txn_id=txn.group(1), quote=s[:280], source_doc=doc_name))
            log.info("%s: операция %s ИСКЛЮЧЕНА из ковенантного периода", scenario, txn.group(1))
            continue

        # 1. Переклассификация.
        if "переклассифицирован" in low or "переквалифицирован" in low:
            to_cat = _category_after(s, "переклассифицирован[а-я]*[^.]{0,60}?")
            if to_cat is None:
                for word, cat in CATEGORY_WORDS.items():
                    if re.search(r"как\s+" + word, low):
                        to_cat = cat
                        break
            cp = re.search(r"контрагенту\s+([^,]+?)\s*,", s)
            facts.adjustments.append(Adjustment(
                kind="reclass", txn_id=txn.group(1) if txn else None,
                amount=money(amounts[0]) if amounts else None,
                counterparty=cp.group(1).strip() if cp else None,
                to_category=to_cat, quote=s[:280], source_doc=doc_name))
            log.info("%s: переклассификация → %s (%s)", scenario, to_cat,
                     txn.group(1) if txn else (cp.group(1) if cp else s[:50]))

    # Таблица разовых статей и порог существенности (обычно приходит со скана).
    m = re.search(r"признаются статьи в сумме не менее\s*" + MONEY, text)
    if m:
        facts.one_off_floor = money(m.group(1))
        log.info("%s: порог существенности разовых статей — %s", scenario, m.group(1))
    for line in text.splitlines():
        m = re.search(r"«([^»]+)»\s*\t?\s*" + MONEY, line)
        if m and facts.one_off_floor is not None:
            facts.one_offs.append((m.group(1), money(m.group(2))))


def apply(facts: ScenarioFacts, txns: list[dict]) -> None:
    """Применяет корректировки к операциям сценария (txns — список объектов Txn)."""
    by_id = {t.txn_id: t for t in txns}

    for a in facts.adjustments:
        if a.kind == "set_amount" and a.txn_id in by_id:
            t = by_id[a.txn_id]
            sign = Decimal(-1) if "расход" in a.quote.lower() else Decimal(1)
            t.amount = sign * a.amount
            t.amount_usd = t.amount
            t.amount_source = f"восстановлена из документа {a.source_doc}"
        elif a.kind == "exclude" and a.txn_id in by_id:
            by_id[a.txn_id].category = "excluded_period"
            by_id[a.txn_id].category_reason = f"исключена аудитором: {a.quote[:120]}"
        elif a.kind == "reclass":
            target = by_id.get(a.txn_id) if a.txn_id else _find_by_amount(txns, a)
            if target is None:
                loud(f"Переклассификация не сопоставлена ни с одной операцией: {a.quote[:120]}")
                continue
            # Запоминаем найденный id: часть документов адресует операцию парой
            # «сумма + контрагент», а для выбора решающей операции нужен именно id.
            a.txn_id = target.txn_id
            if a.to_category:
                target.category = a.to_category
                target.category_reason = f"переклассифицирована аудитором ({a.source_doc}): {a.quote[:120]}"


def _find_by_amount(txns: list, a: Adjustment):
    """Реклассификация без txn_id адресуется парой «сумма + контрагент»."""
    if a.amount is None:
        return None
    cands = [t for t in txns if t.amount is not None and abs(t.amount) == a.amount]
    if a.counterparty and len(cands) > 1:
        key = a.counterparty.lower()[:14]
        cands = [t for t in cands if key in t.counterparty.lower()] or cands
    return cands[0] if len(cands) == 1 else None
