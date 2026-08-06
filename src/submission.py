"""Сборка submission.json строго по шаблону.

Ключи берутся ТОЛЬКО из шаблона: ячейки не добавляются, не переименовываются и не
удаляются — пропущенная или лишняя ячейка не засчитывается. Заполняются ровно три поля.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

from .logger import log, loud

VALID = {"COMPLIANT", "BREACH"}


def build(template: dict, answers: dict[str, dict[str, dict]], cfg: dict) -> dict:
    out = copy.deepcopy(template)
    out["team"] = cfg.get("team", "")
    out["contact_email"] = cfg.get("contact_email", "")
    out["model"] = cfg.get("model", "")

    filled = 0
    for scen, clauses in out.get("answers", {}).items():
        for clause, cell in clauses.items():
            got = answers.get(scen, {}).get(clause)
            if got is None:
                loud(f"Ячейка {scen} п.{clause} осталась незаполненной — она будет оценена в 0.")
                continue
            status = got.get("status")
            actual = got.get("actual")
            if status not in VALID:
                loud(f"Ячейка {scen} п.{clause}: недопустимый status={status!r}")
            if isinstance(actual, Decimal):
                actual = float(actual)
            if actual is None:
                loud(f"Ячейка {scen} п.{clause}: actual не вычислен.")
            cell["status"] = status
            cell["actual"] = actual
            cell["evidence_txn_id"] = got.get("evidence_txn_id")
            filled += 1

    total = sum(len(v) for v in out.get("answers", {}).values())
    log.info("Заполнено ячеек: %d из %d", filled, total)
    return out


def validate_against_template(template: dict, submission: dict) -> list[str]:
    """Структура сабмита должна совпадать с шаблоном ключ-в-ключ."""
    problems: list[str] = []
    t_ans, s_ans = template.get("answers", {}), submission.get("answers", {})
    if set(t_ans) != set(s_ans):
        problems.append(f"набор сценариев отличается: лишние {set(s_ans) - set(t_ans)}, "
                        f"пропущенные {set(t_ans) - set(s_ans)}")
    for scen in t_ans:
        if set(t_ans[scen]) != set(s_ans.get(scen, {})):
            problems.append(f"{scen}: набор пунктов отличается от шаблона")
        for clause, cell in s_ans.get(scen, {}).items():
            if set(cell) != {"status", "actual", "evidence_txn_id"}:
                problems.append(f"{scen} п.{clause}: набор полей ячейки отличается от шаблона")
            if cell.get("status") not in VALID:
                problems.append(f"{scen} п.{clause}: status={cell.get('status')!r}")
            if not isinstance(cell.get("actual"), (int, float)):
                problems.append(f"{scen} п.{clause}: actual не является числом")
    return problems


def write(submission: dict, path: Path) -> None:
    path.write_text(json.dumps(submission, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("Сабмит записан: %s", path)
