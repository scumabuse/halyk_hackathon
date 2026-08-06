#!/usr/bin/env python
"""Локальная оценка submission.json по официальной формуле из CASE.

Формула (раздел «Оценка»):
  status           0.50  — точное совпадение "COMPLIANT"/"BREACH"; неверный статус обнуляет ВСЮ ячейку;
  actual           0.30  — по убывающей шкале: 0.30 * max(0, 1 - e/0.05), e = |ваше - ключ| / |ключ|;
  evidence_txn_id  0.20  — точное совпадение с ключом; если в ключе null,
                           эти 0.20 убывают вместе с actual по той же шкале.

Ячейки взвешиваются по сложности, но веса организаторами не публикуются,
поэтому итог печатается без весов (равные веса) — это нижняя оценка сверху.

Запуск:  python evaluate.py --submission submission.json --key <файл-с-ключом>
Ключ можно не указывать: он ищется в --data по структуре JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

W_STATUS, W_ACTUAL, W_EVIDENCE = 0.50, 0.30, 0.20
CUTOFF = 0.05  # относительная погрешность, при которой actual даёт ноль
VALID = {"COMPLIANT", "BREACH"}


def actual_fraction(got, key) -> float:
    """Доля от максимума за actual: 1.0 при точном совпадении, 0.0 при ошибке >= 5%."""
    if not isinstance(got, (int, float)) or isinstance(got, bool):
        return 0.0
    try:
        key = float(key)
    except (TypeError, ValueError):
        return 0.0
    if key == 0:
        return 1.0 if float(got) == 0.0 else 0.0
    err = abs(float(got) - key) / abs(key)
    return max(0.0, 1.0 - err / CUTOFF)


def score_cell(got: dict | None, key: dict) -> tuple[float, str]:
    """Возвращает (балл 0..1, пояснение) для одной ячейки."""
    if not isinstance(got, dict):
        return 0.0, "ячейка отсутствует или имеет неверный тип"

    status, k_status = got.get("status"), key.get("status")
    if status not in VALID:
        return 0.0, f"status={status!r} не является COMPLIANT/BREACH"
    if status != k_status:
        return 0.0, f"status={status} != ключ {k_status} — вся ячейка обнулена"

    frac = actual_fraction(got.get("actual"), key.get("actual"))
    score = W_STATUS + W_ACTUAL * frac

    k_ev = key.get("evidence_txn_id")
    if k_ev is None:
        # В ключе null: 0.20 не начисляются автоматически, а следуют шкале actual.
        score += W_EVIDENCE * frac
        ev_note = f"evidence(ключ null) по шкале actual: {W_EVIDENCE * frac:.3f}"
    elif got.get("evidence_txn_id") == k_ev:
        score += W_EVIDENCE
        ev_note = "evidence точно"
    else:
        ev_note = f"evidence {got.get('evidence_txn_id')!r} != {k_ev!r}"

    note = f"actual {got.get('actual')} vs {key.get('actual')} (доля {frac:.3f}); {ev_note}"
    return score, note


def load_key(path: Path | None, data_dir: Path | None) -> dict:
    """Ключ ищется по структуре JSON, а не по имени файла (имена в наборе перепутаны)."""
    candidates = [path] if path else []
    if data_dir:
        candidates += sorted(data_dir.rglob("*"))
    for p in candidates:
        if not p or not p.is_file():
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("scenarios"), dict):
            inner = next(iter(obj["scenarios"].values()), None)
            if isinstance(inner, dict) and "covenants" in inner:
                print(f"Ключ ответов: {p.name}")
                return obj["scenarios"]
    raise SystemExit("Ключ ответов не найден — локальная оценка невозможна")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submission", default="submission.json", type=Path)
    ap.add_argument("--key", type=Path, default=None)
    ap.add_argument("--data", type=Path, default=Path("data/agentic-bank-public"))
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    sub = json.loads(a.submission.read_text(encoding="utf-8"))
    key = load_key(a.key, a.data)
    answers = sub.get("answers", {})

    total, rows = 0.0, []
    for scen in sorted(key, key=lambda s: (s[0], int(s[1:]) if s[1:].isdigit() else 0)):
        for clause, kcell in sorted(key[scen]["covenants"].items()):
            got = answers.get(scen, {}).get(clause)
            s, note = score_cell(got, kcell)
            total += s
            rows.append((scen, clause, s, note))

    n = len(rows)
    if not a.quiet:
        print(f"\n{'ячейка':<10} {'балл':>6}   пояснение")
        print("-" * 108)
        for scen, clause, s, note in rows:
            mark = "OK " if s >= 0.999 else ("!! " if s == 0 else "~  ")
            print(f"{mark}{scen:<7}{clause:<4} {s:>6.3f}   {note}")

    perfect = sum(1 for *_, s, _ in ((r[0], r[1], r[2], r[3]) for r in rows) if s >= 0.999)
    zero = sum(1 for r in rows if r[2] == 0)
    print("-" * 108)
    print(f"ИТОГО: {total:.3f} / {n}  =  {100 * total / n:.2f}%   "
          f"(идеальных ячеек {perfect}/{n}, нулевых {zero})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
