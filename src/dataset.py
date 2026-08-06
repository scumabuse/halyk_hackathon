"""Загрузка датасета ПО СОДЕРЖИМОМУ, а не по именам файлов.

В публичном наборе имена файлов перепутаны: леджер лежит в CASE.ru.md,
шаблон сабмита — в master_ledger_2025.csv, а ответы — в submission_template.json.
Расширения тоже врут (PDF под именами .txt/.csv/_Thumbs.db). Поэтому каждый файл
опознаётся по сигнатуре и структуре, а имя используется только для логов.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .logger import log

PDF_MAGIC = b"%PDF-"
LEDGER_COLUMNS = {"txn_id", "date", "account_id", "counterparty", "description", "amount", "currency"}


@dataclass
class RawFile:
    """Один файл датасета вместе с вердиктом о том, чем он на самом деле является."""

    path: Path
    sha256: str
    kind: str  # pdf | ledger | template | answer_key | case | junk
    text: str = ""
    payload: object = None
    pages: int = 0

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Dataset:
    root: Path
    ledger: list[dict] = field(default_factory=list)
    template: dict | None = None
    answer_key: dict | None = None
    case_text: str = ""
    documents: list[RawFile] = field(default_factory=list)
    junk: list[RawFile] = field(default_factory=list)


def _sniff_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _sniff_ledger(text: str) -> list[dict] | None:
    """CSV считается леджером, только если в шапке есть все обязательные колонки."""
    head = text[:2000].splitlines()
    if not head:
        return None
    try:
        cols = set(next(csv.reader(io.StringIO(head[0]))))
    except Exception:
        return None
    if not LEDGER_COLUMNS.issubset(cols):
        return None
    return list(csv.DictReader(io.StringIO(text)))


def _classify_json(obj) -> str | None:
    """Шаблон и ключ ответов различаются структурой, а не именем файла."""
    if not isinstance(obj, dict):
        return None
    if "answers" in obj and isinstance(obj.get("answers"), dict):
        return "template"
    if "scenarios" in obj and isinstance(obj.get("scenarios"), dict):
        inner = next(iter(obj["scenarios"].values()), None)
        if isinstance(inner, dict) and "covenants" in inner:
            return "answer_key"
    return None


def load_dataset(root: Path) -> Dataset:
    """Обходит папку датасета (включая вложенные) и раскладывает файлы по типам."""
    import fitz  # локальный импорт: PyMuPDF нужен только здесь

    ds = Dataset(root=root)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    log.info("Найдено файлов в датасете: %d (папка %s)", len(files), root)

    for path in files:
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()

        if raw[:5] == PDF_MAGIC:
            try:
                with fitz.open(stream=raw, filetype="pdf") as doc:
                    pages = doc.page_count
                    text = "\n".join(pg.get_text() for pg in doc)
            except Exception as exc:  # повреждённый PDF — не роняем весь запуск
                log.warning("PDF не открылся, пропущен: %s (%s)", path.name, exc)
                ds.junk.append(RawFile(path, sha, "junk"))
                continue
            ds.documents.append(RawFile(path, sha, "pdf", text=text, pages=pages))
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            log.info("Бинарный файл без текста — отброшен как мусор: %s", path.name)
            ds.junk.append(RawFile(path, sha, "junk"))
            continue

        obj = _sniff_json(raw)
        jkind = _classify_json(obj)
        if jkind == "template":
            ds.template = obj
            log.info("Шаблон сабмита опознан по структуре: %s", path.name)
            continue
        if jkind == "answer_key":
            ds.answer_key = obj
            log.info("Найден ключ ответов (только для локальной оценки): %s", path.name)
            continue

        rows = _sniff_ledger(text)
        if rows is not None:
            ds.ledger = rows
            log.info("Леджер опознан по колонкам: %s (%d строк)", path.name, len(rows))
            continue

        if text.lstrip().startswith("# Halyk AI Challenge"):
            # Условий задачи может быть несколько (ru/kz) — берём самое длинное.
            if len(text) > len(ds.case_text):
                ds.case_text = text
            log.info("Описание кейса: %s", path.name)
            continue

        ds.junk.append(RawFile(path, sha, "junk", text=text))
        log.info("Файл не относится к делу (мусор/лог): %s", path.name)

    if not ds.ledger:
        raise SystemExit("КРИТИЧНО: в датасете не найден леджер транзакций")
    if ds.template is None:
        raise SystemExit("КРИТИЧНО: в датасете не найден шаблон сабмита")

    log.info(
        "Итог разбора: PDF-документов %d, мусора %d, ключ ответов %s",
        len(ds.documents), len(ds.junk), "есть" if ds.answer_key else "отсутствует",
    )
    return ds


def scenario_of(txn_id: str) -> str | None:
    """scenario_id — это префикс между 'TXN-' и последним дефисом: TXN-P10-0062 -> P10."""
    m = re.match(r"^TXN-(.+)-(\d+)$", txn_id.strip())
    return m.group(1) if m else None


def template_cells(template: dict) -> list[tuple[str, str]]:
    """Список ячеек (scenario, clause) строго в порядке шаблона — источник истины."""
    return [(s, c) for s, cov in template.get("answers", {}).items() for c in cov]


def to_decimal(value: str) -> Decimal:
    return Decimal(str(value).strip())
