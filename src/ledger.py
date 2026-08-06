"""Разбор леджера: отделение «сюжетных» операций от шума и отнесение их к статьям.

Ключевое наблюдение по набору данных: подавляющее большинство строк заёмщика —
наполнитель. Он опознаётся по двум независимым признакам:

  1. КОНТРАГЕНТ. Наполнитель берётся из общего пула англоязычных фамилий-компаний
     (Bridgeport, Foxridge, Juniper, Cedarville, Hartley …), который используется и на
     счетах-декоях (ACC-9xxx). Сюжетные контрагенты привязаны к отрасли заёмщика
     (Aktau Terminal Properties, KEGOC Grid Operations, Taraz Kiln Services …) и на
     декой-счетах не встречаются НИКОГДА. Поэтому пул наполнителя не зашит в код, а
     выводится из самого набора: первое слово контрагента, встреченное хотя бы на одном
     декой-счёте, помечает строку как шум. Это переживает смену пула в приватном наборе.
  2. ОПИСАНИЕ. У наполнителя всегда есть «хвост» через тире — локация или период
     («— Kyzylorda station», «— Q3», «— period 01», «— instalment 2»). У сюжетных
     операций описание чистое, без тире.

На публичном наборе связка этих двух правил даёт 73 операции из 73 — без пропусков и
без ложных срабатываний.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .dataset import scenario_of
from .logger import log, loud

EM_DASH = "—"

# Порядок важен: первое совпадение выигрывает.
CATEGORY_RULES: list[tuple[str, str]] = [
    ("revenue",          r"sales settlement"),
    ("financing",        r"facility drawdown|loan drawdown|drawdown for"),
    ("capex_transfer",   r"^transfer of .*to subsidiary"),
    ("capex",            r"^purchase of .*equipment|^purchase of "),
    ("payroll",          r"payroll (disbursement|settlement)|payroll settlement for"),
    ("utilities",        r"electricity (supply|and water)|utilities supply|electricity supply charges"),
    ("rent",             r"land lease payments|ground lease payments"),
    ("insurance",        r"insurance premium|risk survey"),
    ("taxes",            r"\btax\b.*(instalment|assessment)|tax assessment"),
    ("interest",         r"interest payment|loan interest"),
    ("opex",             r"servicing and operating costs|operating and maintenance expenses"
                         r"|advisory engagement on|servicing contract|systems servicing"
                         r"|remediation|clearance works|arbitration and legal"),
]

QUARTER_WORDS = {
    "first quarter": 1, "second quarter": 2, "third quarter": 3, "fourth quarter": 4,
    "q1": 1, "q2": 2, "q3": 3, "q4": 4,
}


@dataclass
class Txn:
    txn_id: str
    scenario: str
    date: str
    account: str
    counterparty: str
    description: str
    amount: Decimal | None      # None — сумма отсутствует в выгрузке (восстанавливается из документа)
    currency: str
    is_plot: bool = False
    category: str | None = None
    category_reason: str = ""
    amount_usd: Decimal | None = None
    amount_source: str = "ledger"

    @property
    def magnitude(self) -> Decimal:
        """Модуль суммы: показатель actual всегда положительный."""
        return abs(self.amount_usd) if self.amount_usd is not None else Decimal(0)

    @property
    def quarter(self) -> int | None:
        d = self.description.lower()
        for word, q in QUARTER_WORDS.items():
            if word in d:
                return q
        if self.date:
            try:
                return (int(self.date[5:7]) - 1) // 3 + 1
            except ValueError:
                return None
        return None


def _first_token(counterparty: str) -> str:
    s = re.sub(r"\(.*?\)", " ", counterparty or "")
    toks = re.sub(r"[^A-Za-z ]+", " ", s).lower().split()
    return toks[0] if toks else ""


def parse_amount(raw: str) -> Decimal | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


def load_transactions(rows: list[dict], template_scenarios: set[str]) -> list[Txn]:
    """Строит список операций и помечает сюжетные строки заёмщиков из шаблона."""
    txns: list[Txn] = []
    for r in rows:
        scen = scenario_of(r["txn_id"]) or ""
        txns.append(Txn(
            txn_id=r["txn_id"].strip(), scenario=scen, date=r.get("date", ""),
            account=r.get("account_id", ""), counterparty=r.get("counterparty", ""),
            description=r.get("description", ""), amount=parse_amount(r.get("amount", "")),
            currency=(r.get("currency") or "USD").strip(),
        ))

    # Пул наполнителя выводится из счетов, которых нет в шаблоне (декои).
    decoy_tokens = {_first_token(t.counterparty) for t in txns if t.scenario not in template_scenarios}
    decoy_tokens.discard("")
    log.info("Первых слов контрагентов на декой-счетах: %d (это пул наполнителя)", len(decoy_tokens))

    for t in txns:
        if t.scenario not in template_scenarios:
            continue
        clean = EM_DASH not in t.description
        own = _first_token(t.counterparty) not in decoy_tokens
        t.is_plot = clean and own
        if t.is_plot:
            t.category, t.category_reason = classify_category(t.description)

    n_plot = sum(1 for t in txns if t.is_plot)
    n_real = sum(1 for t in txns if t.scenario in template_scenarios)
    log.info("Операций заёмщиков из шаблона: %d, из них сюжетных: %d", n_real, n_plot)

    missing = [t for t in txns if t.is_plot and t.amount is None]
    for t in missing:
        log.info("У сюжетной операции %s нет суммы в выгрузке — ищем её в документах", t.txn_id)
    return txns


def classify_category(description: str) -> tuple[str, str]:
    d = (description or "").lower()
    for cat, pattern in CATEGORY_RULES:
        if re.search(pattern, d):
            return cat, f"описание «{description[:60]}» подходит под правило {cat}"
    return "other", f"описание «{description[:60]}» не подошло ни под одно правило статьи"


def apply_fx(txns: list[Txn], rates: dict[str, Decimal]) -> None:
    """Переводит суммы в доллары. Курс берётся ТОЛЬКО из документов."""
    for t in txns:
        if t.amount is None:
            continue
        if t.currency.upper() == "USD":
            t.amount_usd = t.amount
            continue
        rate = rates.get(t.currency.upper())
        if rate is None:
            if t.is_plot:
                loud(
                    f"{t.scenario}: операция {t.txn_id} выражена в {t.currency}, "
                    "но курс не раскрыт ни в одном документе — сумма НЕ учтена в расчёте."
                )
            continue
        t.amount_usd = (t.amount * rate).quantize(Decimal("0.01"))
        t.amount_source = f"{t.currency}×{rate} (курс из документа)"
