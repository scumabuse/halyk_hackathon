"""Разбор Статьи 6 действующего договора в вычислимую спецификацию ковенанта.

Типов ковенантов в наборе много (в публичном — восемнадцать различных формул), и они
меняются от заёмщика к заёмщику. Поэтому формула не «зашивается по номеру пункта»:
пункт опознаётся по характерной формулировке, а результатом разбора становится ДЕРЕВО
ВЫРАЖЕНИЯ над статьями расходов/доходов. Считает его потом engine.py в Decimal.

Так добавление нового типа ковенанта в приватном наборе сводится к одной строке в
SPEC_LIBRARY, а не к правке расчётного кода.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .logger import log, loud

RE_ARTICLE6 = re.compile(r"Статья\s*6\s*—\s*Финансовые ковенанты(.*?)(?=Статья\s*7\s*—)", re.S)
RE_CLAUSE = re.compile(r"Пункт\s+(\d+\.\d+)\s*(.*?)(?=Пункт\s+\d+\.\d+|\Z)", re.S)
RE_RATIO = re.compile(r"(\d+(?:\.\d+)?)\s*x")
RE_MONEY = re.compile(r"\$([0-9][0-9,]*\.\d{2})")

MIN_WORDS = re.compile(r"не менее|не ниже|не может быть ниже|минимальн", re.I)


# ---- строители узлов выражения -------------------------------------------------
def S(*cats, quarter=None):
    return {"agg": "sum", "cats": list(cats), "quarter": quarter}


def DIV(l, r):  return {"op": "div", "l": l, "r": r}
def ADD(*a):    return {"op": "add", "args": list(a)}
def SUB(l, r):  return {"op": "sub", "l": l, "r": r}
def MAX(*a):    return {"op": "max", "args": list(a)}
def CONST(k):   return {"const": k}


EBITDA = SUB(S("revenue"), S("opex"))

# (маркер в тексте пункта, тип, выражение показателя)
SPEC_LIBRARY: list[tuple[str, str, dict]] = [
    (r"коэффициент капиталоёмкости",                          "capital_intensity",
     DIV(S("capex"), ADD(S("opex"), S("rent")))),
    (r"доля платежей связанным сторонам в операционных расходах",  "rp_over_opex",
     DIV(S("related_party"), S("opex"))),
    (r"покрытие расходов на персонал и коммунальные услуги выручкой", "revenue_cover_payroll_util",
     DIV(S("revenue"), ADD(S("payroll"), S("utilities")))),
    (r"Ограниченные платежи в пользу аффилированных лиц|аффилированным лицам.{0,400}от выручки",
     "rp_over_revenue", DIV(S("related_party"), S("revenue"))),
    (r"выручки и поступлений\s*по\s*финансированию|Minimum Cover of Applications by Sources",
     "cover_sources", DIV(ADD(S("revenue"), S("financing")), ADD(S("opex"), S("capex")))),
    (r"Springing Drawdown Leverage Test|поступлений\s*по\s*финансированию к EBITDA",
     "springing_leverage", DIV(S("financing"), EBITDA)),
    (r"Скорректированной EBITDA к Выручке|скорректированная рентабельность по EBITDA",
     "adj_ebitda_margin", DIV(ADD(SUB(S("revenue"), S("opex")), CONST("addbacks")), S("revenue"))),
    (r"налоговой и коммунальной нагрузки к EBITDA",           "tax_util_over_ebitda",
     DIV(ADD(S("taxes"), S("utilities")), EBITDA)),
    (r"Совокупные обязательства по персоналу|совокупные обязательства по персоналу",
     "personnel_liabilities", ADD(S("payroll"), CONST("severance"))),
    (r"активов, переданных неограниченным дочерним организациям", "unrestricted_transfers",
     DIV(S("capex_transfer_unrestricted"), ADD(S("capex"), S("capex_transfer")))),
    (r"Страховых премий к сумме Арендных и Коммунальных расходов", "insurance_cover",
     DIV(S("insurance"), ADD(S("rent"), S("utilities")))),
    (r"за вычетом наибольшей из величин Расходов на оплату труда и Налогов",
     "revenue_less_largest_overhead", SUB(S("revenue"), MAX(S("payroll"), S("taxes")))),
    (r"Коэффициент покрытия процентов",                       "interest_cover",
     DIV(SUB(S("revenue"), S("opex")), S("interest"))),
    (r"Individual Overhead Line Ceiling|отдельная статья накладных расходов",
     "overhead_line_ceiling", MAX(S("payroll"), S("utilities"))),
    (r"Выручку за четвёртый финансовый квартал|Выручка за четвёртый квартал",
     "q4_revenue", S("revenue", quarter=4)),
    (r"капитальных затрат Группы к EBITDA",                   "group_capex_over_ebitda",
     DIV(CONST("group_capex"), EBITDA)),
    # Наиболее общие формулировки — в конце, чтобы не перехватывать частные случаи выше.
    (r"статье «Капитальные затраты»|Ограничение расходов по статье «Капитальные затраты»",
     "capex_cap", S("capex", "capex_transfer")),
    (r"статье «Выручка»|Минимальная выручка по категории",    "min_revenue", S("revenue")),
    (r"платеж[а-я]* связанным сторонам|платежи Заёмщика.{0,80}связанным сторонам",
     "rp_absolute", S("related_party")),
]


@dataclass
class Covenant:
    clause: str
    kind: str
    expr: dict
    direction: str            # min | max
    threshold: Decimal
    quote: str
    source_doc: str
    condition: dict | None = None       # {"expr":…, "op":">", "value":Decimal}
    notes: list[str] = field(default_factory=list)


def split_clauses(agreement_text: str) -> list[tuple[str, str]]:
    m = RE_ARTICLE6.search(agreement_text)
    body = m.group(1) if m else agreement_text
    out = []
    for cm in RE_CLAUSE.finditer(body):
        text = re.sub(r"\s*\n\s*", " ", cm.group(2)).strip()
        text = re.sub(r"\s{2,}", " ", text)
        out.append((cm.group(1), text))
    return out


def parse(agreement_text: str, doc_name: str, scenario: str) -> dict[str, Covenant]:
    covs: dict[str, Covenant] = {}
    for clause, text in split_clauses(agreement_text):
        kind, expr = None, None
        for pattern, k, e in SPEC_LIBRARY:
            if re.search(pattern, text, re.I):
                kind, expr = k, e
                break
        if kind is None:
            loud(f"{scenario} п.{clause}: тип ковенанта не распознан — "
                 f"«{text[:110]}…». Требуется правило в SPEC_LIBRARY.")
            continue

        direction = "min" if MIN_WORDS.search(text) else "max"
        ratios = [Decimal(x) for x in RE_RATIO.findall(text)]
        moneys = [Decimal(x.replace(",", "")) for x in RE_MONEY.findall(text)]

        condition = None
        if kind == "springing_leverage":
            threshold = ratios[0] if ratios else Decimal(0)
            if moneys:
                condition = {"expr": S("financing"), "op": ">", "value": moneys[0]}
        elif ratios and not moneys:
            threshold = ratios[0]
        elif moneys and not ratios:
            threshold = moneys[0]
        elif ratios and moneys:
            # У долевых ковенантов порог — коэффициент; денежные суммы там встречаются
            # только в оговорках и порогах существенности.
            threshold = ratios[0] if "div" in str(expr) or kind.endswith("_ratio") else moneys[0]
        else:
            loud(f"{scenario} п.{clause}: не найден числовой порог — «{text[:110]}…»")
            continue

        cov = Covenant(clause=clause, kind=kind, expr=expr, direction=direction,
                       threshold=threshold, quote=text[:400], source_doc=doc_name,
                       condition=condition)
        if "переклассифицированной статье как в числителе, так и в знаменателе" in text:
            cov.notes.append("переклассифицированные суммы учитываются в обеих частях дроби")
        if "рассмотренные и отклонённые" in text or "отклонённые аудиторами" in text:
            cov.notes.append("рассмотренные и отклонённые реклассификации в расчёт не принимаются")
        covs[clause] = cov
        log.info("%s п.%s: тип=%s, порог %s (%s)", scenario, clause, kind, threshold, direction)
    return covs
