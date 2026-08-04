from app.core.extractors.base import Chapter
from app.core.pruner import Region, _remap_chapters


def test_deletion_inside_chapter_shifts_its_end_and_following_chapter() -> None:
    chapters = [
        Chapter("第一章", 1, 0, 50),
        Chapter("第二章", 1, 50, 100),
    ]
    regions = [Region(10, 20, "duplicate", "章内删除")]

    mapped = _remap_chapters(chapters, regions, text_len=100, pruned="x" * 90)

    assert [(chapter.start_char, chapter.end_char) for chapter in mapped] == [
        (0, 40),
        (40, 90),
    ]
    assert mapped[0].end_char == mapped[1].start_char

