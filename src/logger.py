"""Логирование: подробный файл + краткая русская сводка в консоль."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("halyk")

_WARN_BANNER: list[str] = []


def setup(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fh = logging.FileHandler(log_dir / "run.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ch)


def loud(message: str) -> None:
    """КРУПНОЕ предупреждение: агент не должен молча подставлять умолчания."""
    _WARN_BANNER.append(message)
    log.warning("!!! %s", message)


def banner_summary() -> list[str]:
    return list(_WARN_BANNER)


def print_banner() -> None:
    if not _WARN_BANNER:
        print("\n[OK] Критических предупреждений нет.")
        return
    line = "=" * 78
    print(f"\n{line}\n  ВНИМАНИЕ — ТРЕБУЕТСЯ ПРОВЕРКА ЧЕЛОВЕКОМ ({len(_WARN_BANNER)})\n{line}")
    for i, m in enumerate(_WARN_BANNER, 1):
        print(f"  {i:>2}. {m}")
    print(line)
