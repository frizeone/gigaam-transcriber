"""Тесты DOCX: OOXML, кириллица, таймкоды, пустой результат."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from docx import Document

from app.asr.base import Segment
from app.docx_render import group_segments, render_docx
from app.jobs import build_preview, make_docx_filename

SEGMENTS = [
    Segment(0.0, 2.0, "Добрый день, коллеги."),
    Segment(2.2, 4.0, "Сегодня мы обсудим результаты."),
    Segment(6.5, 8.0, "Начнём с первого вопроса."),
    Segment(8.2, 10.0, "Он касается сроков проекта."),
    Segment(10.2, 12.0, "Срок — конец месяца."),
    Segment(12.2, 14.0, "Есть ли вопросы?"),
    Segment(14.2, 16.0, "Тогда продолжаем."),
]


def _render(tmp_path: Path, *, timestamps: bool, segments: list[Segment] | None = None) -> Path:
    return render_docx(
        tmp_path / "result.docx",
        filename="встреча.mp4",
        segments=SEGMENTS if segments is None else segments,
        duration_s=16.0,
        model="GigaAM v3 E2E RNNT (gigaam-v3-e2e-rnnt)",
        language="Русский (ru-RU)",
        processed_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        processing_seconds=1.8,
        rtf=0.1125,
        image_version="latest (v0.1.0)",
        timestamps=timestamps,
    )


def test_group_segments_by_pause_and_size() -> None:
    groups = group_segments(SEGMENTS)
    assert len(groups) == 2
    assert groups[0].segment_count == 2
    assert groups[0].text == "Добрый день, коллеги. Сегодня мы обсудим результаты."
    assert groups[1].segment_count == 5
    many = [Segment(i * 1.0, i * 1.0 + 0.5, f"Фраза {i}.") for i in range(13)]
    grouped = group_segments(many)
    assert [group.segment_count for group in grouped] == [6, 6, 1]


def test_docx_valid_and_contains_cyrillic(tmp_path: Path) -> None:
    path = _render(tmp_path, timestamps=False)
    assert path.is_file()
    assert zipfile.is_zipfile(path)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert "word/document.xml" in archive.namelist()
        assert "word/styles.xml" in archive.namelist()
    doc = Document(str(path))
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Транскрибация: встреча.mp4" in text
    assert "Добрый день, коллеги." in text
    assert "[00:00:00]" not in text
    metadata = {row.cells[0].text: row.cells[1].text for row in doc.tables[0].rows}
    assert metadata["Файл"] == "встреча.mp4"
    assert metadata["Модель"].startswith("GigaAM v3 E2E RNNT")
    assert metadata["Язык"] == "Русский (ru-RU)"
    assert "[00:00:00]" not in metadata


def test_docx_timestamps_and_language(tmp_path: Path) -> None:
    path = _render(tmp_path, timestamps=True)
    doc = Document(str(path))
    paragraphs = [paragraph.text for paragraph in doc.paragraphs]
    assert any(text.startswith("[00:00:00] ") for text in paragraphs)
    assert any(text.startswith("[00:00:06] ") for text in paragraphs)
    assert 'w:val="ru-RU"' in doc.styles.element.xml
    assert 'w:lang' in doc.styles.element.xml


def test_docx_page_numbers_footer(tmp_path: Path) -> None:
    path = _render(tmp_path, timestamps=False)
    doc = Document(str(path))
    xml = doc.sections[0].footer._element.xml
    assert "PAGE" in xml
    assert "NUMPAGES" in xml
    assert "Страница" in xml
    assert "updateFields" in doc.settings.element.xml


def test_docx_empty_result(tmp_path: Path) -> None:
    path = _render(tmp_path, timestamps=False, segments=[])
    doc = Document(str(path))
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Речь не обнаружена." in text


def test_docx_filename_and_preview() -> None:
    name = make_docx_filename("../../etc/passwd.mp4", datetime(2026, 1, 2, 3, 4, 5))
    assert name == "passwd__2026-01-02_03-04.docx"
    assert build_preview([Segment(0, 1, "Привет")], 500) == "Привет"
    assert build_preview([], 500) == "Речь не обнаружена."
    long_preview = build_preview([Segment(0, 1, "а" * 600)], 500)
    assert len(long_preview) == 500
    assert long_preview.endswith("…")
