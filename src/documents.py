"""Классификация документов и выбор действующей редакции.

Имена файлов непрозрачны и намеренно перепутаны, поэтому тип документа определяется
только по содержимому. Ловушки, которые здесь обезвреживаются:

  * «НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ (2024 г.) … НЕ ПРИМЕНЯЕТСЯ» — старый договор с ДРУГИМИ
    порогами (например, у ACC-7801 порог 6.1 равен 1.17x вместо 0.42x: по недействующей
    редакции реальное нарушение выглядело бы соблюдением);
  * «ПРОЕКТ — ПРОМЕЖУТОЧНАЯ ВЕДОМОСТЬ … НЕ ЯВЛЯЕТСЯ ОКОНЧАТЕЛЬНОЙ ПОЗИЦИЕЙ АУДИТОРА» —
    черновик аудита, чьи переклассификации применять нельзя;
  * процедурные комплаенс-регламенты, которые похожи на KYC-досье, но прямо сообщают,
    что не содержат выводов о конкретном клиенте.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logger import log, loud

RE_ACCOUNT = re.compile(r"ACC-\d{4}(?!-)")            # ACC-7801-05 из декоя не считается
RE_PERIOD = re.compile(r"с (\d{4})-\d{2}-\d{2} по (\d{4})-\d{2}-\d{2}")
RE_VOID = re.compile(r"НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ|НЕ ПРИМЕНЯЕТСЯ")
RE_DRAFT = re.compile(r"ПРОЕКТ\s*—\s*ПРОМЕЖУТОЧНАЯ|НЕ ЯВЛЯЕТСЯ ОКОНЧАТЕЛЬНОЙ")
RE_ARTICLE6 = re.compile(r"Статья\s*6\s*—\s*Финансовые ковенанты")

# Регламент, маскирующийся под досье: сам заявляет, что выводов о клиенте не содержит.
RE_PROCEDURE = re.compile(
    r"не содержит заключений о каком-либо конкретном клиенте"
    r"|не является клиентским досье"
    r"|методическое руководство"
)


@dataclass
class Doc:
    name: str
    text: str
    doc_type: str = "other"
    account: str | None = None
    is_void: bool = False
    is_draft: bool = False
    period_years: tuple[str, str] | None = None
    reject_reason: str | None = None
    scanned_pages: list[int] = field(default_factory=list)


def classify(name: str, text: str) -> Doc:
    d = Doc(name=name, text=text)

    accounts = sorted(set(RE_ACCOUNT.findall(text)))
    d.account = accounts[0] if len(accounts) == 1 else None

    d.is_void = bool(RE_VOID.search(text))
    d.is_draft = bool(RE_DRAFT.search(text))
    m = RE_PERIOD.search(text)
    if m:
        d.period_years = (m.group(1), m.group(2))

    has_art6 = bool(RE_ARTICLE6.search(text))
    if "ДОГОВОР БАНКОВСКОГО ЗАЙМА" in text or has_art6:
        d.doc_type = "agreement"
    elif RE_PROCEDURE.search(text):
        d.doc_type = "procedure_decoy"
    elif "Досье" in text and "KYC" in text:
        d.doc_type = "kyc"
    elif ("Примечания к финансовой отчётности" in text
          # Выводы по классификации могут лежать не в примечаниях, а в отдельном отчёте
          # о согласованных процедурах — он прямо назван окончательной позицией аудитора
          # и заменяет промежуточные ведомости.
          or "согласованных процедур" in text
          or "аудитор" in text.lower()[:600]):
        d.doc_type = "audit_draft" if d.is_draft else "audit_final"
    elif "казначейств" in text.lower():
        d.doc_type = "treasury"
    return d


@dataclass
class ScenarioDocs:
    scenario: str
    account: str
    agreement: Doc | None = None
    audits: list[Doc] = field(default_factory=list)
    kyc: Doc | None = None
    treasury: list[Doc] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (имя, причина)


def build_registry(docs: list[Doc], acc2scen: dict[str, str], ledger_year: str) -> dict[str, ScenarioDocs]:
    """Раскладывает документы по сценариям и выбирает действующие редакции."""
    reg = {s: ScenarioDocs(scenario=s, account=a) for a, s in acc2scen.items()}

    for d in docs:
        scen = acc2scen.get(d.account or "")
        if scen is None:
            continue
        sd = reg[scen]

        if d.doc_type == "agreement":
            if d.is_void:
                sd.rejected.append((d.name, "стоит штамп «НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ … НЕ ПРИМЕНЯЕТСЯ»"))
            elif d.period_years and d.period_years[0] != ledger_year:
                sd.rejected.append((d.name, f"ковенантный период {d.period_years[0]} ≠ году леджера {ledger_year}"))
            elif sd.agreement is None:
                sd.agreement = d
            else:
                sd.rejected.append((d.name, "дубликат действующего договора"))
        elif d.doc_type == "audit_final":
            sd.audits.append(d)
        elif d.doc_type == "audit_draft":
            sd.rejected.append((d.name, "черновик аудита («ПРОЕКТ — ПРОМЕЖУТОЧНАЯ ВЕДОМОСТЬ»), позиция не окончательная"))
        elif d.doc_type == "kyc":
            sd.kyc = d
        elif d.doc_type == "treasury":
            sd.treasury.append(d)
        elif d.doc_type == "procedure_decoy":
            sd.rejected.append((d.name, "комплаенс-регламент, а не досье: сам заявляет, что выводов о клиенте не содержит"))

    for scen, sd in sorted(reg.items()):
        log.info(
            "%-4s счёт %s: договор=%s, финальных аудитов=%d, KYC=%s, отброшено=%d",
            scen, sd.account, sd.agreement.name if sd.agreement else "НЕ НАЙДЕН",
            len(sd.audits), sd.kyc.name if sd.kyc else "нет", len(sd.rejected),
        )
        for nm, why in sd.rejected:
            log.info("        отброшен %s — %s", nm, why)
        if sd.agreement is None:
            loud(
                f"{scen}: не найден ДЕЙСТВУЮЩИЙ договор за {ledger_year} год. "
                "Пороги ковенантов будут взяты из недействующей редакции (если она есть) — "
                "это заведомо ненадёжно, требуется проверка человеком."
            )
    return reg
