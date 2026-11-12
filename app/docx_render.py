"""Формирование валидного DOCX (OOXML) с кириллицей и нумерацией страниц."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.document import Document as DocumentType
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.asr.base import Segment
from app.media import format_duration_hms, format_timestamp

FONT_NAME = "Calibri"
FONT_SIZE = 11
PARAGRAPH_MAX_SEGMENTS = 6
PAUSE_SPLIT_S = 1.5


@dataclass(slots=True)
class ParagraphGroup:
    start: float
    text: str
    segment_count: int


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if right[0] in ",.;:!?)]}»":
        return f"{left}{right}"
    if left.endswith(("-", "—", "/", "(")):
        return f"{left}{right}"
    return f"{left} {right}"


def group_segments(
    segments: list[Segment],
    *,
    max_segments: int = PARAGRAPH_MAX_SEGMENTS,
    pause_s: float = PAUSE_SPLIT_S,
) -> list[ParagraphGroup]:
    """Склеить сегменты в абзацы: пауза > pause_s или каждые max_segments."""
    groups: list[ParagraphGroup] = []
    current_text = ""
    current_start = 0.0
    current_count = 0
    previous_end: float | None = None
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        should_split = bool(
            current_text
            and (
                current_count >= max(1, max_segments)
                or (previous_end is not None and segment.start - previous_end > pause_s)
            )
        )
        if should_split:
            groups.append(ParagraphGroup(start=current_start, text=current_text, segment_count=current_count))
            current_text = ""
            current_count = 0
        if not current_text:
            current_start = segment.start
        current_text = _join_text(current_text, text)
        current_count += 1
        previous_end = segment.end
    if current_text:
        groups.append(ParagraphGroup(start=current_start, text=current_text, segment_count=current_count))
    return groups


def _set_rfonts(rpr: Any, font_name: str = FONT_NAME) -> None:
    """Прописать шрифт для ascii/hAnsi/cs/eastAsia (кириллица входит в hAnsi)."""
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), font_name)


def _set_lang(rpr: Any, lang: str = "ru-RU") -> None:
    language = rpr.find(qn("w:lang"))
    if language is None:
        language = OxmlElement("w:lang")
        rpr.append(language)
    language.set(qn("w:val"), lang)
    language.set(qn("w:eastAsia"), lang)
    language.set(qn("w:bidi"), "ar-SA")


def _style_document(doc: DocumentType) -> None:
    # w:lang на уровне docDefaults — Word не будет подчёркивать кириллицу.
    for rpr in doc.styles.element.xpath("w:docDefaults/w:rPrDefault/w:rPr"):
        _set_rfonts(rpr)
        _set_lang(rpr)

    normal = doc.styles["Normal"]
    normal.font.name = FONT_NAME
    normal.font.size = Pt(FONT_SIZE)
    _set_rfonts(normal.element.get_or_add_rPr())
    _set_lang(normal.element.get_or_add_rPr())
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.space_before = Pt(0)


def _add_page_field(paragraph: Any, instruction: str, placeholder: str = "1") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" {instruction} "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = placeholder
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(separate)
    run._r.append(text)
    run._r.append(end)


def _add_footer(doc: DocumentType) -> None:
    section = doc.sections[0]
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(0)
    run = paragraph.add_run("Страница ")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    _add_page_field(paragraph, "PAGE")
    mid = paragraph.add_run(" из ")
    mid.font.size = Pt(9)
    mid.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    _add_page_field(paragraph, "NUMPAGES")
    for r in paragraph.runs:
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)


def _add_metadata_table(doc: DocumentType, rows: list[tuple[str, str]]) -> None:
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    for label, value in rows:
        cells = table.add_row().cells
        label_paragraph = cells[0].paragraphs[0]
        label_run = label_paragraph.add_run(label)
        label_run.bold = True
        label_run.font.size = Pt(10)
        value_paragraph = cells[1].paragraphs[0]
        value_run = value_paragraph.add_run(value)
        value_run.font.size = Pt(10)
        for paragraph in (label_paragraph, value_paragraph):
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.space_before = Pt(2)
            paragraph.paragraph_format.line_spacing = 1.0
    widths = (Cm(4.2), Cm(12.6))
    for row in table.rows:
        for index, width in enumerate(widths):
            row.cells[index].width = width
    doc.add_paragraph()


def render_docx(
    output_path: Path,
    *,
    filename: str,
    segments: list[Segment],
    duration_s: float,
    model: str,
    language: str,
    processed_at: datetime,
    processing_seconds: float,
    rtf: float | None,
    image_version: str,
    timestamps: bool,
) -> Path:
    """Создать .docx. Всегда возвращает валидный OOXML, даже при тишине."""
    doc = Document()
    _style_document(doc)

    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    section.footer_distance = Cm(1.0)

    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(14)
    title.paragraph_format.line_spacing = 1.0
    title_run = title.add_run(f"Транскрибация: {filename}")
    title_run.bold = True
    title_run.font.size = Pt(20)
    title_run.font.name = FONT_NAME
    title_run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
    _set_rfonts(title_run._element.get_or_add_rPr())

    rtf_text = f"{rtf:.3f}×" if rtf is not None else "—"
    metadata_rows = [
        ("Файл", filename),
        ("Длительность", format_duration_hms(duration_s)),
        ("Дата обработки", processed_at.strftime("%d.%m.%Y %H:%M:%S %Z").strip()),
        ("Модель", model),
        ("Язык", language),
        ("Время обработки", format_duration_hms(processing_seconds, with_ms=True)),
        ("RTF (скорость)", rtf_text),
        ("Версия образа", image_version),
    ]
    _add_metadata_table(doc, metadata_rows)

    groups = group_segments(segments)
    if not groups:
        empty = doc.add_paragraph()
        empty.alignment = WD_ALIGN_PARAGRAPH.CENTER
        empty.paragraph_format.space_before = Pt(18)
        run = empty.add_run("Речь не обнаружена.")
        run.italic = True
        run.font.size = Pt(12)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    else:
        for group in groups:
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.first_line_indent = Cm(1.0)
            if timestamps:
                stamp = paragraph.add_run(f"[{format_timestamp(group.start)}] ")
                stamp.bold = True
                stamp.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            paragraph.add_run(group.text)

    _add_footer(doc)
    update_fields = OxmlElement("w:updateFields")
    update_fields.set(qn("w:val"), "true")
    doc.settings.element.append(update_fields)

    doc.core_properties.title = f"Транскрибация: {filename}"
    doc.core_properties.subject = "Расшифровка речи GigaAM v3 E2E"
    doc.core_properties.author = "GigaAM Transcriber"
    doc.core_properties.comments = "Сформировано локальным сервисом GigaAM Transcriber"
    doc.core_properties.language = "ru-RU"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
