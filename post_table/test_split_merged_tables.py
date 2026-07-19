"""合并逻辑子表拆分规则的通用结构测试。"""

from post_table.split_merged_tables import split_merged_tables_in_markdown


PARENT_HEADER = (
    "<tr><td>Pin Name</td><td>Device-X</td><td>Device-X</td>"
    "<td>I/O Type</td><td>Description</td></tr>"
)
CHILD_HEADER = (
    "<tr><td>Pin Name</td><td>Package-A</td><td>Package-B</td>"
    "<td>I/O Type</td><td>Description</td></tr>"
)


def _table(*rows: str) -> str:
    return "<table class=\"source\">" + "".join(rows) + "</table>"


def _section(name: str) -> str:
    return f'<tr><td colspan="5">{name}</td></tr>'


def _data(name: str, value_a: str, value_b: str) -> str:
    return (
        f"<tr><td>{name}</td><td>{value_a}</td><td>{value_b}</td>"
        "<td>I</td><td>data</td></tr>"
    )


def test_split_full_width_section_followed_by_complete_repeated_header():
    source = _table(
        _section("Section A"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_A", "1", "2"),
        _section("Section B"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_B", "3", "4"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 1
    assert result.count("<table") == 2
    assert result.count('class="source"') == 2
    assert "Section A" in result and "Section B" in result


def test_split_complete_repeated_header_without_section_title():
    source = _table(
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_A", "1", "2"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_B", "3", "4"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 1
    assert result.count("<table") == 2


def test_do_not_split_section_without_repeated_header():
    source = _table(
        _section("Section A"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_A", "1", "2"),
        _section("Notes"),
        _data("SIG_B", "3", "4"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 0
    assert result == source


def test_do_not_split_partially_repeated_multilevel_header():
    changed_child_header = CHILD_HEADER.replace("Package-B", "Package-C")
    source = _table(
        _section("Section A"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_A", "1", "2"),
        _section("Section B"),
        PARENT_HEADER,
        changed_child_header,
        _data("SIG_B", "3", "4"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 0
    assert result == source


def test_do_not_split_only_matching_child_layer_after_changed_parent_layer():
    """多级表头第一层有格式差异时，不能只按相同的第二层拆表。"""

    changed_parent_header = PARENT_HEADER.replace("Device-X", "Device X")
    source = _table(
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_A", "1", "2"),
        changed_parent_header,
        CHILD_HEADER,
        _data("SIG_B", "3", "4"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 0
    assert result == source


def test_split_multiple_logical_sections():
    source = _table(
        _section("Section A"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_A", "1", "2"),
        _section("Section B"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_B", "3", "4"),
        _section("Section C"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_C", "5", "6"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 2
    assert result.count("<table") == 3


def test_do_not_split_when_rowspan_crosses_boundary():
    source = _table(
        _section("Section A"),
        PARENT_HEADER,
        CHILD_HEADER,
        '<tr><td rowspan="3">SIG_A</td><td>1</td><td>2</td>'
        "<td>I</td><td>data</td></tr>",
        _section("Section B"),
        PARENT_HEADER,
        CHILD_HEADER,
        _data("SIG_B", "3", "4"),
    )

    result, added = split_merged_tables_in_markdown(source)

    assert added == 0
    assert result == source
