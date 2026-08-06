#!/usr/bin/env python
"""Агент проверки ковенантов — Halyk AI Challenge.

Запуск:  python main.py --data data/agentic-bank-public --out submission.json
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import yaml

from src import adjustments as adj
from src import covenants as cov_mod
from src import documents as docs_mod
from src import evidence as ev_mod
from src import ledger as led
from src import ocr as ocr_mod
from src import related_parties as rp_mod
from src import submission as sub_mod
from src.dataset import load_dataset, scenario_of
from src.engine import build_context, compute, r2
from src.logger import banner_summary, log, loud, print_banner, setup

ROOT = Path(__file__).resolve().parent


def scenario_sort_key(s: str):
    return (s[0], int(s[1:]) if s[1:].isdigit() else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка ковенантов по кредитным договорам")
    ap.add_argument("--data", type=Path, required=True, help="папка распакованного датасета")
    ap.add_argument("--out", type=Path, default=Path("submission.json"))
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    ap.add_argument("--report", type=Path, default=ROOT / "report.md")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    setup(ROOT / "logs", a.verbose)
    cfg = yaml.safe_load(a.config.read_text(encoding="utf-8")) if a.config.is_file() else {}

    print("=" * 78)
    print("  АГЕНТ ПРОВЕРКИ КОВЕНАНТОВ — Halyk AI Challenge")
    print("=" * 78)

    # --- Этап 1. Датасет (опознание файлов по содержимому) ----------------------
    ds = load_dataset(a.data)
    template_scenarios = set(ds.template.get("answers", {}))
    log.info("Сценарии из шаблона: %s", ", ".join(sorted(template_scenarios, key=scenario_sort_key)))

    txns_all = led.load_transactions(ds.ledger, template_scenarios)
    acc2scen: dict[str, str] = {}
    for t in txns_all:
        if t.scenario in template_scenarios and t.account:
            acc2scen.setdefault(t.account, t.scenario)
    years = sorted({t.date[:4] for t in txns_all if t.date})
    ledger_year = max(set(years), key=lambda y: sum(1 for t in txns_all if t.date.startswith(y)))
    log.info("Год леджера: %s. Соответствие счёт→сценарий: %s", ledger_year,
             ", ".join(f"{k}→{v}" for k, v in sorted(acc2scen.items())))

    # --- Этап 2. Документы: текст + распознание сканов + классификация -----------
    import fitz
    overlay = ROOT / "overlays" / "scanned_pages.json"
    classified: list[docs_mod.Doc] = []
    for rf in ds.documents:
        text = rf.text
        try:
            with fitz.open(rf.path) as doc:
                extra, _ = ocr_mod.recover_text(doc, rf.name, overlay, client=None)
        except Exception:
            extra = ""
        if extra:
            text = text + "\n" + extra
        classified.append(docs_mod.classify(rf.name, text))
    reg = docs_mod.build_registry(classified, acc2scen, ledger_year)

    # --- Этап 3..8. По каждому сценарию -----------------------------------------
    answers: dict[str, dict[str, dict]] = {}
    report: list[str] = ["# Отчёт агента проверки ковенантов\n"]

    for scen in sorted(template_scenarios, key=scenario_sort_key):
        sd = reg.get(scen)
        if sd is None:
            loud(f"{scen}: нет ни одного документа — ячейки останутся незаполненными")
            continue
        txns = [t for t in txns_all if t.scenario == scen]
        report.append(f"\n## Сценарий {scen} (счёт {sd.account})\n")

        # 3.1 Договор: действующая редакция, иначе — недействующая с предупреждением
        agreement, agreement_note = sd.agreement, ""
        if agreement is None:
            fallback = next((d for d in classified
                             if d.account == sd.account and d.doc_type == "agreement"), None)
            if fallback is not None:
                agreement = fallback
                agreement_note = ("действующая редакция за %s год ОТСУТСТВУЕТ; пороги взяты из "
                                  "недействующей редакции — требуется проверка человеком" % ledger_year)
                loud(f"{scen}: {agreement_note} ({fallback.name})")
        if agreement is None:
            loud(f"{scen}: договор не найден вовсе — ячейки не могут быть заполнены обоснованно")
            continue
        covs = cov_mod.parse(agreement.text, agreement.name, scen)
        report.append(f"- Договор: `{agreement.name}`"
                      + (f" — **{agreement_note}**" if agreement_note else "")
                      + f", ковенантный период {agreement.period_years}\n")
        for nm, why in sd.rejected:
            report.append(f"- Отброшен `{nm}` — {why}\n")

        # 3.2 Аудиторские факты (только окончательные документы)
        facts = adj.ScenarioFacts()
        for d in sd.audits + sd.treasury:
            adj.parse_audit(d.text, d.name, scen, facts)

        # 3.3 Связанные стороны из KYC
        parties, unrestricted = rp_mod.extract_parties(sd.kyc.text if sd.kyc else "", scen)
        if not parties and any(c.kind in ("rp_absolute", "rp_over_opex", "rp_over_revenue")
                               for c in covs.values()):
            loud(f"{scen}: есть ковенант по связанным сторонам, но досье KYC с долями не найдено — "
                 "круг связанных сторон определить нечем.")
        unrestricted_keys = {rp_mod.normalise(p.name) for p in unrestricted}

        # 3.4 Валюта, корректировки, связанные стороны
        led.apply_fx(txns, facts.fx_rates)
        adj.apply(facts, txns)
        adjusted_ids = {x.txn_id for x in facts.adjustments if x.txn_id}

        borrower = _borrower_name(agreement.text)
        fallback_ids = set()
        if not any(p.is_related for p in parties):
            fallback_ids = rp_mod.fallback_related(txns, borrower, scen)
        for t in txns:
            if not t.is_plot or t.amount_usd is None or t.amount_usd >= 0:
                continue
            if t.txn_id in adjusted_ids:
                continue          # переклассификация аудитора имеет приоритет
            if t.txn_id in fallback_ids or rp_mod.match_counterparty(t.counterparty, parties):
                t.category = "related_party"
                t.category_reason = "контрагент признан связанной стороной (досье KYC / МСФО 24)"

        addbacks = None
        if facts.one_off_floor is not None:
            addbacks = sum((amt for _, amt in facts.one_offs if amt >= facts.one_off_floor),
                           Decimal(0))
            log.info("%s: обратно добавляемые разовые статьи = %s (порог %s)",
                     scen, addbacks, facts.one_off_floor)

        ctx = build_context(txns, facts.disclosed, addbacks, unrestricted_keys, scen)

        # 3.5 Расчёт, доказательство
        answers[scen] = {}
        for clause in sorted(covs, key=lambda c: [int(x) for x in c.split(".")]):
            cv = covs[clause]
            res = compute(cv, ctx, scen)
            actual, status = res.actual, res.status

            if actual is None:
                # Показатель не выводится из доступных доказательств. Сертифицировать
                # соблюдение в таком случае нельзя — фиксируем нарушение и кричим.
                status = "BREACH"
                actual = _proxy_value(cv, ctx)
                loud(f"{scen} п.{clause}: показатель не вычислим по имеющимся документам. "
                     f"Проставлено BREACH (соблюдение не подтверждено) и приблизительное "
                     f"actual={actual}. ТРЕБУЕТСЯ ПРОВЕРКА ЧЕЛОВЕКОМ.")

            ev, ev_log = ev_mod.select(cv, res, txns, adjusted_ids, facts.disclosed,
                                       addbacks, unrestricted_keys, scen)
            answers[scen][clause] = {"status": status,
                                     "actual": float(actual) if actual is not None else None,
                                     "evidence_txn_id": ev}
            report.append(
                f"\n### {scen} п.{clause} — {cv.kind}\n"
                f"- Цитата: «{cv.quote[:220]}»\n"
                f"- Порог: {'≤' if cv.direction == 'max' else '≥'} {cv.threshold}\n"
                f"- Расчёт: {res.trace}\n"
                f"- Вердикт: **{status}**, actual = {actual}\n"
                f"- Доказательство: {ev or 'null'}\n"
                + "".join(f"    - {l}\n" for l in ev_log)
            )

    # --- Этап 9. Сабмит ----------------------------------------------------------
    submission = sub_mod.build(ds.template, answers, cfg)
    problems = sub_mod.validate_against_template(ds.template, submission)
    for p in problems:
        loud(f"Проверка структуры сабмита: {p}")
    sub_mod.write(submission, a.out)
    a.report.write_text("".join(report), encoding="utf-8")
    log.info("Отчёт с обоснованиями: %s", a.report)

    print(f"\nЗаполнено сценариев: {len(answers)}; файл: {a.out}")
    print(f"Пояснения по каждой ячейке: {a.report}")
    print_banner()
    return 0


def _borrower_name(agreement_text: str) -> str:
    """Наименование заёмщика из преамбулы договора («… заключён между X (…)»)."""
    import re
    m = re.search(r"между\s+([A-Z][A-Za-z0-9&.\- ]+?(?:JSC|LLP|LLC|Ltd))", agreement_text)
    if m:
        return m.group(1).strip()
    m = re.search(r"Заёмщик[,\s]+([A-Z][A-Za-z0-9&.\- ]+?(?:JSC|LLP|LLC|Ltd))", agreement_text)
    return m.group(1).strip() if m else ""


def _proxy_value(cv, ctx) -> Decimal:
    """Грубая замена, когда точный показатель не выводится: считаем по тому, что есть."""
    from src.engine import evaluate
    for key in list(ctx.missing_consts):
        ctx.consts.setdefault(key, ctx.sums.get("capex", Decimal(0)))
    val = evaluate(cv.expr, ctx, [])
    return r2(val) if val is not None else Decimal("0.00")


if __name__ == "__main__":
    sys.exit(main())
