"""Работа со страницами-картинками (сканами) внутри PDF.

В наборе встречаются страницы, у которых нет текстового слоя: ключевая таблица
(доли голосующих прав в KYC, разовые статьи EBITDA) отрисована растром. Обычный
экстрактор возвращает по ним пустоту, и ковенант тихо считается по неполным данным.

Порядок действий:
  1. страница помечается как «скан», если картинок > 0, а текста < MIN_CHARS;
  2. текст берётся из первого доступного источника:
       overlay  — заранее выверенная человеком расшифровка, ключ = хеш пикселей;
       tesseract — если установлен (PyMuPDF отдаёт get_textpage_ocr);
       LLM-vision — если задан ANTHROPIC_API_KEY (см. llm_client);
  3. если ни один источник не сработал — КРУПНОЕ предупреждение с именем файла и
     номером страницы. Молча продолжать нельзя: цифры ковенанта окажутся неверными.

Ключ overlay — sha256 первых байт растра страницы при фиксированном DPI, поэтому он
не зависит ни от имени файла, ни от порядка страниц.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .logger import log, loud

MIN_CHARS = 120  # меньше этого на странице с картинкой — считаем сканом
RENDER_DPI = 150

_OVERLAY: dict[str, str] | None = None


def page_hash(page, dpi: int = RENDER_DPI) -> str:
    pm = page.get_pixmap(dpi=dpi)
    return hashlib.sha256(pm.samples).hexdigest()[:16]


def _load_overlay(overlay_path: Path) -> dict[str, str]:
    global _OVERLAY
    if _OVERLAY is None:
        if overlay_path.is_file():
            _OVERLAY = json.loads(overlay_path.read_text(encoding="utf-8"))
            log.info("Загружено выверенных расшифровок сканов: %d", len(_OVERLAY))
        else:
            _OVERLAY = {}
    return _OVERLAY


def _try_tesseract(page) -> str | None:
    try:
        tp = page.get_textpage_ocr(language="rus+eng", dpi=200, full=True)
        text = page.get_text(textpage=tp).strip()
        return text or None
    except Exception:
        return None


def _try_llm_vision(page, client) -> str | None:
    if client is None or not client.enabled:
        return None
    png = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
    return client.read_page_image(png)


def scanned_pages(doc) -> list[int]:
    """Индексы страниц без текстового слоя, но с изображением."""
    return [
        i for i, pg in enumerate(doc)
        if len(pg.get_text().strip()) < MIN_CHARS and pg.get_images()
    ]


def recover_text(doc, doc_name: str, overlay_path: Path, client=None) -> tuple[str, list[str]]:
    """Возвращает (дополнительный_текст, список_нерасшифрованных_страниц)."""
    overlay = _load_overlay(overlay_path)
    parts: list[str] = []
    unresolved: list[str] = []

    for idx in scanned_pages(doc):
        page = doc[idx]
        h = page_hash(page)
        if h in overlay:
            parts.append(overlay[h])
            log.info("Скан-страница %s стр.%d распознана из выверенной расшифровки (%s)",
                     doc_name, idx + 1, h)
            continue
        text = _try_tesseract(page)
        if text:
            parts.append(text)
            log.info("Скан-страница %s стр.%d распознана через Tesseract", doc_name, idx + 1)
            continue
        text = _try_llm_vision(page, client)
        if text:
            parts.append(text)
            log.info("Скан-страница %s стр.%d распознана через LLM-vision", doc_name, idx + 1)
            continue
        unresolved.append(f"{doc_name} стр.{idx + 1} (хеш {h})")

    if unresolved:
        loud(
            "Не распознаны страницы-сканы (нет ни расшифровки, ни Tesseract, ни ANTHROPIC_API_KEY): "
            + "; ".join(unresolved)
            + ". Показатели ковенантов по этим заёмщикам могут быть посчитаны по неполным данным."
        )
    return "\n".join(parts), unresolved
