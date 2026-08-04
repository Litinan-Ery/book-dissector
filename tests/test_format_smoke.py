from pathlib import Path

import fitz

from app.core.extractors import md, pdf, txt


def test_markdown_preserves_heading_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "fixture.md"
    source.write_text("# 第一章\n正文甲。\n\n## 第一节\n正文乙。\n", encoding="utf-8")

    result = md.extract(source)

    assert result.ok
    assert [chapter.title for chapter in result.chapters] == ["第一章", "第一节"]
    assert result.chapters[0].end_char == result.chapters[1].start_char


def test_txt_detects_chapters(tmp_path: Path) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("夹具书\n\n第一章 起点\n正文甲。\n\n第二章 继续\n正文乙。\n", encoding="utf-8")

    result = txt.extract(source)

    assert result.ok
    assert [chapter.title for chapter in result.chapters] == ["第一章 起点", "第二章 继续"]
    assert result.chapters[0].end_char == result.chapters[1].start_char


def test_pdf_extracts_text_or_reports_scan_warning(tmp_path: Path) -> None:
    source = tmp_path / "fixture.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Fixture PDF body " * 12)
    document.save(source)
    document.close()

    result = pdf.extract(source)

    assert result.ok
    assert "Fixture PDF body" in result.text

