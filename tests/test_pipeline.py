"""Тесты разбора документов, леджера, досье и структуры сабмита."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from src import documents as docs
from src import related_parties as rp
from src import submission as sub
from src.covenants import parse as parse_covenants
from src.ledger import classify_category, load_transactions
from src.adjustments import ScenarioFacts, parse_audit

ROOT = Path(__file__).resolve().parents[1]


# ---- документы -----------------------------------------------------------------
def test_void_edition_is_detected():
    d = docs.classify("x.pdf", "НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ (2024 г.). … НЕ ПРИМЕНЯЕТСЯ. "
                              "ДОГОВОР БАНКОВСКОГО ЗАЙМА № ACC-7805")
    assert d.is_void and d.doc_type == "agreement" and d.account == "ACC-7805"


def test_draft_audit_is_detected():
    d = docs.classify("x.pdf", "ПРОЕКТ — ПРОМЕЖУТОЧНАЯ ВЕДОМОСТЬ. НЕ ЯВЛЯЕТСЯ ОКОНЧАТЕЛЬНОЙ "
                               "ПОЗИЦИЕЙ АУДИТОРА. Примечания к финансовой отчётности ACC-7801")
    assert d.is_draft and d.doc_type == "audit_draft"


def test_compliance_procedure_is_not_a_kyc_dossier():
    """Регламент маскируется под досье, но сам заявляет, что выводов о клиенте нет."""
    d = docs.classify("x.pdf", "Досье «Знай своего клиента» KYC ACC-7801 … "
                               "он не является клиентским досье и не содержит определений")
    assert d.doc_type == "procedure_decoy"


def test_decoy_subaccount_does_not_bind_document():
    d = docs.classify("x.pdf", "смежные вспомогательные счета вида ACC-7801-05 рассматриваются")
    assert d.account is None


def test_registry_rejects_void_and_picks_active():
    active = docs.classify("act.pdf", "ДОГОВОР БАНКОВСКОГО ЗАЙМА ACC-7801 "
                                      "Ковенантного периода с 2025-01-01 по 2025-12-31")
    void = docs.classify("void.pdf", "НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ (2024 г.) НЕ ПРИМЕНЯЕТСЯ "
                                     "ДОГОВОР БАНКОВСКОГО ЗАЙМА ACC-7801 "
                                     "Ковенантного периода с 2024-01-01 по 2024-12-31")
    reg = docs.build_registry([active, void], {"ACC-7801": "P1"}, "2025")
    assert reg["P1"].agreement.name == "act.pdf"
    assert any("НЕДЕЙСТВУЮЩАЯ" in why for _, why in reg["P1"].rejected)


# ---- леджер --------------------------------------------------------------------
@pytest.mark.parametrize("desc,expected", [
    ("Purchase of quayside crane equipment", "capex"),
    ("Berth servicing and operating costs 2025", "opex"),
    ("Port handling and stevedoring sales settlement 2025", "revenue"),
    ("Plant crew payroll disbursement 2025", "payroll"),
    ("Electricity supply charges 2025", "utilities"),
    ("Terminal land lease payments 2025", "rent"),
    ("Term loan facility drawdown for cold store expansion", "financing"),
    ("Transfer of processing plant equipment to subsidiary", "capex_transfer"),
    ("Property insurance premium 2025", "insurance"),
    ("Corporate income tax instalment 2025", "taxes"),
])
def test_category_rules(desc, expected):
    assert classify_category(desc)[0] == expected


def test_noise_rows_are_not_plot():
    """Наполнитель отличается «хвостом» через тире и контрагентом из общего пула."""
    rows = [
        {"txn_id": "TXN-P1-0010", "date": "2025-05-21", "account_id": "ACC-7801",
         "counterparty": "Ural Crane Works LLP",
         "description": "Purchase of quayside crane equipment", "amount": "-1842006.44",
         "currency": "USD"},
        {"txn_id": "TXN-P1-0002", "date": "2025-01-11", "account_id": "ACC-7801",
         "counterparty": "Foxridge Tax Advisory Corp (Semey base)",
         "description": "Telecom leased line — Kostanay centre, February 2025",
         "amount": "-1474435.73", "currency": "USD"},
        {"txn_id": "TXN-9001-0036", "date": "2025-01-01", "account_id": "ACC-9001",
         "counterparty": "Foxridge Catering", "description": "Digital media buy",
         "amount": "-366837.86", "currency": "USD"},
    ]
    txns = load_transactions(rows, {"P1"})
    by_id = {t.txn_id: t for t in txns}
    assert by_id["TXN-P1-0010"].is_plot is True
    assert by_id["TXN-P1-0002"].is_plot is False


def test_missing_amount_is_none_not_zero():
    rows = [{"txn_id": "TXN-P7-0033", "date": "2025-11-18", "account_id": "ACC-7807",
             "counterparty": "State Revenue Committee",
             "description": "Mineral extraction tax assessment 2025", "amount": "",
             "currency": "USD"}]
    assert load_transactions(rows, {"P7"})[0].amount is None


# ---- досье KYC -----------------------------------------------------------------
KYC = """Бенефициарное владение и контроль
Организация
Доля голосующих прав
Aktobe Elevator Services LLP
28.6%
Aral Capital Partners, LLP
33.4%
Организации, в которых Группа владеет 30.0% и более голосующих прав, признаются
связанными сторонами для целей Договора.
Идентификация и проверка сведений
"""


def test_kyc_threshold_is_read_from_the_dossier_not_hardcoded():
    parties, _ = rp.extract_parties(KYC, "P4")
    by = {p.name: p for p in parties}
    assert by["Aral Capital Partners, LLP"].is_related is True      # 33.4% >= 30.0%
    assert by["Aktobe Elevator Services LLP"].is_related is False   # 28.6% <  30.0%


def test_two_tables_are_not_mixed():
    """Доли голосующих прав и доли активов в залоге — разные таблицы с разными правилами."""
    text = KYC + """Обеспечительное покрытие дочерних организаций
Дочерняя организация
Доля активов в залоге
Zhezkazgan Conveyor Assets LLP
87.6%
Zhezkazgan Processing Holdings LLP
11.4%
Дочерние организации, у которых доля активов в залоге ниже 50.0%, находятся вне периметра
обеспечения и для целей Договора рассматриваются как неограниченные.
"""
    parties, unrestricted = rp.extract_parties(text, "P9")
    names = {p.name for p in parties if p.is_related}
    assert "Zhezkazgan Conveyor Assets LLP" not in names    # это залог, а не голоса
    assert [p.name for p in unrestricted] == ["Zhezkazgan Processing Holdings LLP"]


def test_counterparty_match_ignores_legal_form_and_branch():
    parties = [rp.Party("«Saryarka Capital Partners» LLP", Decimal("42.3"), True, "")]
    assert rp.match_counterparty("Saryarka Capital Partners LLP (Almaty office)", parties)
    assert rp.match_counterparty("Bridgeport Catering Trust", parties) is None


# ---- аудиторские корректировки --------------------------------------------------
def test_rejected_reclassification_is_not_applied():
    facts = ScenarioFacts()
    parse_audit("(7.2) Операция TXN-P10-0012, первоначально учтённая как Операционные расходы "
                "($118,447.52), рассматривалась на предмет возможной переклассификации как "
                "Страховые премии; первоначальная классификация сохраняется, и корректировка "
                "для целей ковенантов не производилась.", "a.pdf", "P10", facts)
    assert [a.kind for a in facts.adjustments] == ["rejected"]


def test_fx_rate_is_derived_from_a_settled_pair():
    facts = ScenarioFacts()
    parse_audit("(9.1) Расчёты: счёт на сумму 72,146.75 EUR урегулирован платежом в долларах "
                "США в размере $83,690.23.", "a.pdf", "P3", facts)
    assert facts.fx_rates["EUR"].quantize(Decimal("0.01")) == Decimal("1.16")


def test_missing_amount_is_recovered_from_document():
    facts = ScenarioFacts()
    parse_audit("(8.1) Операция TXN-P8-0031: сумма не отражена в выгрузке реестра; фактическая "
                "сумма операции составляет $884,204.16 (расход).", "a.pdf", "P8", facts)
    a = facts.adjustments[0]
    assert a.kind == "set_amount" and a.amount == Decimal("884204.16")


def test_period_cutoff_exclusion():
    facts = ScenarioFacts()
    parse_audit("(9.1) Операция TXN-B4-0026, датированная 2025-11-20, исключена из ковенантного "
                "периода 2025 года.", "a.pdf", "B4", facts)
    assert facts.adjustments[0].kind == "exclude"


# ---- сабмит ---------------------------------------------------------------------
TEMPLATE = {"team": "", "contact_email": "", "model": "",
            "answers": {"P1": {"6.1": {"status": None, "actual": None, "evidence_txn_id": None}}}}


def test_submission_matches_template_keys_exactly():
    out = sub.build(TEMPLATE, {"P1": {"6.1": {"status": "BREACH", "actual": 0.46,
                                              "evidence_txn_id": None}}},
                    {"team": "essence", "contact_email": "e@x", "model": "m"})
    assert sub.validate_against_template(TEMPLATE, out) == []
    assert out["answers"]["P1"]["6.1"]["actual"] == 0.46
    assert out["team"] == "essence"


def test_submission_flags_bad_status():
    out = sub.build(TEMPLATE, {"P1": {"6.1": {"status": "breach", "actual": 1.0,
                                              "evidence_txn_id": None}}}, {})
    assert any("status" in p for p in sub.validate_against_template(TEMPLATE, out))


def test_submission_is_valid_json_with_cyrillic():
    out = sub.build(TEMPLATE, {"P1": {"6.1": {"status": "BREACH", "actual": 0.46,
                                              "evidence_txn_id": "TXN-P1-0045"}}}, {})
    assert json.loads(json.dumps(out, ensure_ascii=False))["answers"]["P1"]["6.1"]["status"] == "BREACH"
