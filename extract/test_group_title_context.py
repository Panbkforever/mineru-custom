"""group 章节标题上下文的通用结构测试。"""

from extract.group_title_context import (
    GroupTitleContextTracker,
    append_group_subtitle,
    join_group_titles,
)
from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
    iter_table_candidates_from_markdown,
)


def test_group_contains_previous_chapter_current_path_and_table_title():
    tracker = GroupTitleContextTracker()
    for heading in (
        "# 4 Previous Chapter",
        "# 4.1 Previous Section",
        "## Previous Unnumbered Subsection",
        "# 5 Current Chapter",
        "# 5.1 Current Section",
    ):
        tracker.observe(heading, require_markdown_heading=True)

    assert tracker.build_group_context("Table 5-1. Pin Description") == (
        "4 Previous Chapter\n"
        "4.1 Previous Section\n"
        "Previous Unnumbered Subsection\n"
        "5 Current Chapter\n"
        "5.1 Current Section\n"
        "Table 5-1. Pin Description"
    )


def test_only_titles_before_current_table_are_included():
    tracker = GroupTitleContextTracker()
    for heading in ("# 7 Device", "# 7.1 Pins"):
        tracker.observe(heading, require_markdown_heading=True)

    first_group = tracker.build_group_context("Table 7-1. First Table")
    tracker.observe("# 7.2 Electrical", require_markdown_heading=True)
    second_group = tracker.build_group_context("Table 7-2. Second Table")

    assert "7.2 Electrical" not in first_group
    assert "7.2 Electrical" in second_group


def test_previous_window_moves_forward_one_chapter_only():
    tracker = GroupTitleContextTracker()
    for heading in (
        "# 3 Chapter Three",
        "# 3.1 Section Three",
        "# 4 Chapter Four",
        "# 4.1 Section Four",
        "# 5 Chapter Five",
    ):
        tracker.observe(heading, require_markdown_heading=True)

    group = tracker.build_group_context("Table 5-1. Current Table")
    assert "3 Chapter Three" not in group
    assert group.splitlines() == [
        "4 Chapter Four",
        "4.1 Section Four",
        "5 Chapter Five",
        "Table 5-1. Current Table",
    ]


def test_table_of_contents_does_not_replace_body_chapter_windows():
    tracker = GroupTitleContextTracker()
    for heading in (
        "# 1 Device Overview",
        "# 1.1 Features",
        "# Table of Contents",
        "# 1 Device Overview .. 1",
        "# 2 Revision History .... 7",
        "# 3 Device Comparison . 8",
        "# 4 Terminal Configuration and Functions ..... 10",
        "# 3 Device Comparison",
    ):
        tracker.observe(heading, require_markdown_heading=True)

    assert tracker.build_group_context("Table 3-1. Device Comparison") == (
        "2 Revision History\n"
        "3 Device Comparison\n"
        "Table 3-1. Device Comparison"
    )


def test_toc_keeps_multiple_levels_for_previous_chapter_fallback():
    tracker = GroupTitleContextTracker()
    for heading in (
        "# Contents",
        "# 4 Configuration .... 20",
        "# 4.1 Pins .... 21",
        "# 4.2 Signals .... 25",
        "# 5 Specifications .... 30",
        "# 5 Specifications",
    ):
        tracker.observe(heading, require_markdown_heading=True)

    assert tracker.build_group_context("Table 5-1. Limits") == (
        "4 Configuration\n"
        "4.1 Pins\n"
        "4.2 Signals\n"
        "5 Specifications\n"
        "Table 5-1. Limits"
    )


def test_first_chapter_has_no_invented_previous_context():
    tracker = GroupTitleContextTracker()
    tracker.observe("# 1 Introduction", require_markdown_heading=True)

    assert tracker.build_group_context("Table 1-1. Overview") == (
        "1 Introduction\nTable 1-1. Overview"
    )


def test_markdown_reader_ignores_plain_toc_lines_but_keeps_table_title():
    markdown = """
4 Old Chapter
4.1 Old Section
# 4 Real Previous Chapter
# 4.1 Real Previous Section
# 5 Real Current Chapter
# 5.1 Real Current Section
Table 5-1. Pin Description (continued)
<table><tr><td>PIN</td><td>NAME</td></tr><tr><td>1</td><td>A</td></tr></table>
"""

    candidates = iter_table_candidates_from_markdown(markdown)

    assert len(candidates) == 1
    assert candidates[0].title == "Table 5-1. Pin Description"
    assert candidates[0].group_context.splitlines() == [
        "4 Real Previous Chapter",
        "4.1 Real Previous Section",
        "5 Real Current Chapter",
        "5.1 Real Current Section",
        "Table 5-1. Pin Description",
    ]


def test_group_cleanup_preserves_lines_and_appends_internal_subtitle():
    base = join_group_titles(
        "# 4 Previous\n# 5 Current",
        "Table 5-1. Pins (continued)",
    )

    assert append_group_subtitle(base, "Power Pins") == (
        "4 Previous\n5 Current\nTable 5-1. Pins\nPower Pins"
    )


def test_complete_context_is_written_to_final_extraction_group():
    html = (
        "<table>"
        "<tr><td>PIN NO.</td><td>PIN NAME</td><td>TYPE</td></tr>"
        "<tr><td>1</td><td>VDD</td><td>Power</td></tr>"
        "<tr><td>2</td><td>GND</td><td>Ground</td></tr>"
        "</table>"
    )
    context = "4 Previous\n5 Current\n5.1 Pins\nTable 5-1. Pin List"

    result = extract_pin_package_info_from_table_candidates(
        [
            TableCandidate(
                html=html,
                page_idx=0,
                title="Table 5-1. Pin List",
                group_context=context,
            )
        ]
    )

    assert result[0]["group_list"][0]["group"] == context
