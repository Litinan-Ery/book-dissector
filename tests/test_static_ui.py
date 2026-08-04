from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stylesheet_contains_only_css_not_shell_or_javascript() -> None:
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")

    assert "cat >" not in css
    assert "echo " not in css
    assert "frontend written" not in css
    assert "const $" not in css


def test_ui_discloses_cloud_processing_and_one_click_pipeline() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

    assert "正文片段会发送给 DeepSeek" in html
    assert "一键拆解" in html
    assert 'id="task-list"' in html


def test_frontend_does_not_build_untrusted_html_strings() -> None:
    javascript = (ROOT / "app/static/app.js").read_text(encoding="utf-8")

    assert ".innerHTML" not in javascript
    assert "cloud_consent" in javascript
    assert "/estimate" in javascript
    assert "/cancel" in javascript
    assert "/retry-failed" in javascript
