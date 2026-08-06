"""Связанные стороны из досье KYC.

Досье НЕ перечисляет связанные стороны готовым списком: оно даёт таблицу долей и
отдельным предложением — порог, начиная с которого контрагент признаётся связанным.
Порог у каждого заёмщика СВОЙ (в публичном наборе встречаются 25.0, 30.0, 35.0, 36.0,
38.0 и 40.0 %), поэтому зашивать «40 %» нельзя: половина ответов уедет.

Отдельный вид таблицы — доля активов в залоге по дочерним организациям: там правило
обратное («ниже 50.0 % — вне периметра обеспечения, считается неограниченной»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz

from .logger import log, loud

RE_PCT_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
RE_INLINE = re.compile(r"^(?P<name>.+?)[\t;]+\s*(?P<pct>\d+(?:\.\d+)?)\s*%\s*$")

RE_THRESHOLD_AT_LEAST = re.compile(
    r"владе[а-я]*\s+(\d+(?:\.\d+)?)\s*%?\s*и более голосующих прав", re.I)
RE_THRESHOLD_BELOW = re.compile(
    r"доля активов в залоге ниже\s+(\d+(?:\.\d+)?)\s*%", re.I)

LEGAL_FORMS = re.compile(
    r"\b(LLP|JSC|LLC|Ltd|Inc|Corp|Co|GmbH|L\.L\.P\.|ТОО|АО|LP)\b\.?", re.I)


@dataclass
class Party:
    name: str
    pct: Decimal
    is_related: bool
    why: str


def normalise(name: str) -> str:
    s = re.sub(r"\(.*?\)", " ", name or "")          # «(Turkistan point)» — филиал, не влияет
    s = s.replace("«", " ").replace("»", " ").replace('"', " ")
    s = LEGAL_FORMS.sub(" ", s)
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _parse_table(text: str) -> list[tuple[str, Decimal]]:
    """Достаёт пары «наименование → доля» из таблицы досье."""
    out: list[tuple[str, Decimal]] = []
    lines = [ln.rstrip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        m = RE_INLINE.match(ln)
        if m:
            out.append((m.group("name").strip(), Decimal(m.group("pct"))))
            continue
        m = RE_PCT_LINE.match(ln)
        if m:
            for prev in range(i - 1, max(-1, i - 4), -1):
                cand = lines[prev].strip()
                if cand and not RE_PCT_LINE.match(cand) and "Доля" not in cand:
                    out.append((cand, Decimal(m.group(1))))
                    break
    return out


def _section(text: str, start_markers: list[str], stop_markers: list[str]) -> str:
    """Вырезает раздел досье: таблиц может быть две, и смешивать их нельзя."""
    low = text.lower()
    starts = [low.find(m.lower()) for m in start_markers]
    starts = [i for i in starts if i >= 0]
    if not starts:
        return ""
    beg = min(starts)
    ends = [low.find(m.lower(), beg + 1) for m in stop_markers]
    ends = [i for i in ends if i > beg]
    return text[beg:min(ends)] if ends else text[beg:]


# Досье содержит ДВЕ похожие таблицы, и путать их нельзя:
#   «Доля голосующих прав»  → кто является связанной стороной (порог «X% и более»);
#   «Доля активов в залоге» → какие дочерние организации вне периметра обеспечения
#                             (обратное правило «ниже X% — неограниченная»).
VOTING_START = ["Бенефициарное владение и контроль", "Доля голосующих прав"]
PLEDGE_START = ["Обеспечительное покрытие дочерних организаций", "Доля активов в залоге"]


def extract_parties(kyc_text: str, scenario: str) -> tuple[list[Party], list[Party]]:
    """Возвращает (связанные стороны, неограниченные дочерние организации)."""
    if not kyc_text:
        return [], []

    related: list[Party] = []
    unrestricted: list[Party] = []

    voting = _section(kyc_text, VOTING_START, PLEDGE_START + ["Идентификация и проверка"])
    rows = _parse_table(voting)
    m = RE_THRESHOLD_AT_LEAST.search(voting)
    if m and rows:
        thr = Decimal(m.group(1))
        log.info("%s: порог признания связанной стороной из досье — %s%%", scenario, thr)
        for name, pct in rows:
            ok = pct >= thr
            why = f"доля голосующих прав {pct}% {'≥' if ok else '<'} порога {thr}% из досье"
            related.append(Party(name, pct, ok, why))
            log.info("        %-42s %5s%%  %s", name, pct, "СВЯЗАННАЯ" if ok else "не связана")
    elif rows:
        loud(f"{scenario}: в досье KYC есть таблица долей, но НЕ найдено предложение с порогом — "
             "связанные стороны определить нельзя.")

    pledge = _section(kyc_text, PLEDGE_START, ["Идентификация и проверка", "Проверка по санкционным"])
    prows = _parse_table(pledge)
    m = RE_THRESHOLD_BELOW.search(pledge)
    if m and prows:
        thr = Decimal(m.group(1))
        log.info("%s: дочерние организации с долей активов в залоге ниже %s%% считаются неограниченными",
                 scenario, thr)
        for name, pct in prows:
            if pct < thr:
                unrestricted.append(Party(name, pct, True, f"доля активов в залоге {pct}% < {thr}%"))
                log.info("        %-42s %5s%%  НЕОГРАНИЧЕННАЯ дочерняя организация", name, pct)

    return related, unrestricted


RE_RETAINER = re.compile(r"management (advisory )?retainer|management retainer fee|retainer fee", re.I)


def fallback_related(txns, borrower_name: str, scenario: str) -> set[str]:
    """Резервное определение связанной стороны, когда досье KYC отсутствует.

    Опора — МСФО (IAS) 24: вознаграждение за управление, выплачиваемое холдинговой
    (капитальной) компании той же группы, является операцией со связанной стороной.
    Поэтому берём платежи с назначением «management advisory retainer», получатель
    которых носит имя группы заёмщика И является холдинговой/капитальной структурой.
    Одного совпадения имени группы недостаточно: у заёмщика бывают и обычные
    операционные компании с тем же названием (например, арендодатель).
    """
    group = (normalise(borrower_name).split() or [""])[0]
    picked: set[str] = set()
    for t in txns:
        if not t.is_plot or t.amount_usd is None or t.amount_usd >= 0:
            continue
        if not RE_RETAINER.search(t.description or ""):
            continue
        cp = normalise(t.counterparty)
        holdingish = re.search(r"holding|capital|group", (t.counterparty or ""), re.I)
        if group and group in cp.split() and holdingish:
            picked.add(t.txn_id)
            log.info("%s: без досье KYC связанной стороной признан «%s» "
                     "(вознаграждение за управление холдингу группы «%s»)",
                     scenario, t.counterparty, group)
    if picked:
        loud(f"{scenario}: досье KYC не найдено — круг связанных сторон определён по МСФО (IAS) 24 "
             f"(вознаграждение за управление холдингу группы). Требуется проверка человеком.")
    return picked


def match_counterparty(counterparty: str, parties: list[Party], min_score: int = 88) -> Party | None:
    """Сопоставляет контрагента леджера со списком из досье (без учёта формы и филиала)."""
    cp = normalise(counterparty)
    best, best_score = None, 0
    for p in parties:
        if not p.is_related:
            continue
        score = max(fuzz.token_sort_ratio(cp, normalise(p.name)),
                    fuzz.partial_ratio(cp, normalise(p.name)))
        if score > best_score:
            best, best_score = p, score
    if best is not None and best_score >= min_score:
        log.info("        контрагент «%s» ↔ связанная сторона «%s» (совпадение %d%%)",
                 counterparty, best.name, best_score)
        return best
    return None
