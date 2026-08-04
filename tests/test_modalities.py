import json
from pathlib import Path

from app import config
from app.core.extractors.base import extract_book
from app.core.modalities import inventory_content
from app.models.domain import Modality


MIXED_MARKDOWN = """# 第一章

正文观点。

| 指标 | 数值 |
| --- | --- |
| 覆盖率 | 99% |

![流程图](flow.png)

公式：$$E = mc^2$$

```python
print("evidence")
```

[^1]: 关键限制条件。
"""


def test_markdown_inventory_detects_every_required_modality() -> None:
    blocks = inventory_content(
        MIXED_MARKDOWN,
        source_fingerprint="d" * 64,
        source_format="md",
    )

    modalities = {block.modality for block in blocks}
    assert modalities >= {
        Modality.TEXT,
        Modality.HEADING,
        Modality.TABLE,
        Modality.IMAGE,
        Modality.FORMULA,
        Modality.CODE,
        Modality.FOOTNOTE,
    }
    assert all(block.source_span.end_char > block.source_span.start_char for block in blocks)


def test_inventory_ids_are_stable_for_the_same_source() -> None:
    first = inventory_content(MIXED_MARKDOWN, "d" * 64, "md")
    second = inventory_content(MIXED_MARKDOWN, "d" * 64, "md")

    assert [block.block_id for block in first] == [block.block_id for block in second]
    assert [block.source_span.source_id for block in first] == [
        block.source_span.source_id for block in second
    ]


def test_image_formula_and_table_are_explicit_warnings() -> None:
    blocks = inventory_content(MIXED_MARKDOWN, "d" * 64, "md")
    risky = {
        block.modality: block.parse_warning
        for block in blocks
        if block.modality in {Modality.IMAGE, Modality.FORMULA, Modality.TABLE}
    }

    assert all(risky[modality] for modality in risky)


def test_extract_book_persists_fingerprint_structure_and_modality_inventory(
    tmp_path: Path, monkeypatch
) -> None:
    books = tmp_path / "books"
    intermediate = tmp_path / "intermediate"
    output = tmp_path / "output"
    runs = tmp_path / "runs"
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", intermediate)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(config, "RUNS_DIR", runs)
    source = tmp_path / "mixed.md"
    source.write_text(MIXED_MARKDOWN, encoding="utf-8")

    result = extract_book("book123", source)

    assert result.ok
    meta = json.loads((books / "book123.meta.json").read_text(encoding="utf-8"))
    assert len(meta["source_fingerprint"]) == 64
    assert meta["structure_report"]["valid"] is True
    assert {block["modality"] for block in meta["content_blocks"]} >= {
        "text",
        "heading",
        "table",
        "image",
        "formula",
        "code",
        "footnote",
    }
    assert meta["modality_warnings"]

