from app.core.modalities import detect_html_modalities, detect_text_modalities


def test_markdown_modalities_are_reported_without_blocking() -> None:
    text = """|列A|列B|\n|---|---|\n|1|2|\n\n![图](a.png)\n\n$$x=1$$\n\n```python\nprint(1)\n```"""
    warnings = detect_text_modalities(text, location="第一章")

    assert {item["type"] for item in warnings} == {"table", "image", "formula", "code"}
    assert all(item["location"] == "第一章" for item in warnings)


def test_epub_html_modalities_include_location_and_message() -> None:
    raw = "<table><tr><td>x</td></tr></table><img src='a.png'><math>x</math><pre>x</pre>"
    warnings = detect_html_modalities(raw, location="chapter.xhtml")

    assert {item["type"] for item in warnings} == {"table", "image", "formula", "code"}
    assert all(item["message"] for item in warnings)
