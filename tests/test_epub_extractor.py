from pathlib import Path

from ebooklib import epub

from app.core.extractors.epub import extract


def _build_epub(path: Path) -> None:
    book = epub.EpubBook()
    book.set_identifier("fixture-epub-multi-heading")
    book.set_title("多标题夹具")
    book.set_language("zh-CN")
    chapter = epub.EpubHtml(title="正文", file_name="chapter.xhtml", lang="zh-CN")
    chapter.content = """
        <html><body>
        <h1>第一章</h1><p>第一章正文。</p>
        <h2>第一节</h2><p>第一节正文。</p>
        </body></html>
    """
    book.add_item(chapter)
    book.toc = (epub.Link("chapter.xhtml", "正文", "chapter"),)
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)


def test_headings_in_same_xhtml_have_distinct_source_offsets(tmp_path: Path) -> None:
    source = tmp_path / "multi-heading.epub"
    _build_epub(source)

    result = extract(source)

    assert result.ok
    assert [chapter.title for chapter in result.chapters] == ["第一章", "第一节"]
    assert result.chapters[0].start_char < result.chapters[1].start_char
    assert result.chapters[0].end_char == result.chapters[1].start_char
    for chapter in result.chapters:
        assert result.text[chapter.start_char :].startswith(chapter.title)
        assert chapter.start_char < chapter.end_char
