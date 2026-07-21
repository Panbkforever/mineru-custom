"""package_resolver 的通用行为测试。

测试只构造抽象表格结构，不使用某一份 PDF 的产品名或固定答案。重点验证：

* 当前表题和当前章节可以提供封装证据，上一章节不能；
* 相同封装家族、不同 drawing code 必须保持为不同实体；
* 多封装绑定、同章节唯一继承和引脚集合关联均在行提取前完成；
* 证据冲突或关联不唯一时 pkg 必须保持为空。
"""

from __future__ import annotations

from dataclasses import dataclass

from extract.package_resolver import (
    PackageTableSource,
    PackageTargetTable,
    build_document_package_registry,
    resolve_document_packages,
)


@dataclass(frozen=True)
class _Column:
    index: int
    raw_header: str
    field_name: str


@dataclass(frozen=True)
class _Binding:
    package: str
    pin_no_column: int
    pin_name_column: int | None
    row_indexes: frozenset[int] | None = None


@dataclass(frozen=True)
class _Plan:
    is_multi_package: bool
    mode: str
    bindings: tuple[_Binding, ...] = ()


PIN_COLUMNS = (
    _Column(0, "BALL NUMBER", "pin_no"),
    _Column(1, "SIGNAL NAME", "pin_name"),
    _Column(2, "TYPE", "type"),
)


def _source(
    table_id: int,
    *,
    title: str = "",
    current: tuple[str, ...] = (),
    previous: tuple[str, ...] = (),
    rows: tuple[tuple[str, ...], ...] = (),
) -> PackageTableSource:
    return PackageTableSource(
        table_id=table_id,
        title=title,
        group_context="\n".join((*previous, *current, title)),
        previous_chapter_titles=previous,
        current_chapter_titles=current,
        rows=rows,
    )


def _target(
    table_id: int,
    *,
    title: str = "",
    current: tuple[str, ...] = (),
    rows: tuple[tuple[str, ...], ...] = (("A1", "SIG_A", "I"),),
    columns: tuple[_Column, ...] = PIN_COLUMNS,
) -> PackageTargetTable:
    return PackageTargetTable(
        table_id=table_id,
        title=title,
        current_chapter_titles=current,
        headers=tuple(column.raw_header for column in columns),
        data_rows=rows,
        columns=columns,
    )


def test_current_table_title_assigns_single_package() -> None:
    source = _source(0, title="Table 5-1. Pin Description - ZCE Package")
    target = _target(0, title=source.title)

    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[target],
        multi_package_plans={},
    )

    assert result.package_label(0) == "ZCE"
    assert result.assignments[0].mode == "table_title"


def test_previous_chapter_package_is_not_inherited() -> None:
    previous = ("4 Package Information - ABC Package",)
    source = _source(0, title="Table 5-1. Signal Description", previous=previous)
    target = _target(0, title=source.title)

    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[target],
        multi_package_plans={},
    )

    assert result.package_label(0) == ""
    assert result.assignments[0].mode == "unresolved"


def test_package_drawing_separates_same_family() -> None:
    package_rows = (
        ("Package Drawing", "Package Type", "Pins"),
        ("ABC", "QFN", "64"),
        ("XYZ", "QFN", "64"),
    )
    registry = build_document_package_registry(
        [_source(0, title="Package Information", rows=package_rows)]
    )

    abc_key = registry.unique_key_for_label("ABC")
    xyz_key = registry.unique_key_for_label("XYZ")
    assert abc_key
    assert xyz_key
    assert abc_key != xyz_key
    # QFN 同时属于两个 drawing，因此不能把家族名唯一解析成其中一个封装。
    assert registry.unique_key_for_label("QFN") == ""


def test_ambiguous_family_title_does_not_create_generic_package() -> None:
    package_rows = (
        ("Package Drawing", "Package Type", "Pins"),
        ("ABC", "QFN", "64"),
        ("XYZ", "QFN", "64"),
    )
    sources = [
        _source(0, title="Package Information", rows=package_rows),
        _source(1, title="Table 3-1. QFN Package Pin Description"),
    ]
    target = _target(1, title=sources[1].title)

    result = resolve_document_packages(
        all_tables=sources,
        target_tables=[target],
        multi_package_plans={},
    )

    assert result.package_label(1) == ""
    assert result.assignments[1].mode == "conflict"
    assert "多个封装" in result.assignments[1].reason


def test_multiple_local_package_values_without_binding_remain_unresolved() -> None:
    rows = (
        ("Package Drawing", "Package Type", "Pins"),
        ("ABC", "QFN", "64"),
        ("XYZ", "QFN", "64"),
    )
    source = _source(0, title="Table 2-1. Package Pin Data", rows=rows)
    target = _target(0, title=source.title)

    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[target],
        multi_package_plans={},
    )

    assert result.package_label(0) == ""
    assert result.assignments[0].mode == "conflict"


def test_unique_package_in_same_section_is_inherited() -> None:
    current = ("5 Pin Functions", "5.2 Signal Tables")
    sources = [
        _source(0, title="Table 5-1. ABC Package Pins", current=current),
        _source(1, title="Table 5-2. Electrical Connections", current=current),
    ]
    targets = [
        _target(0, title=sources[0].title, current=current),
        _target(1, title=sources[1].title, current=current),
    ]

    result = resolve_document_packages(
        all_tables=sources,
        target_tables=targets,
        multi_package_plans={},
    )

    assert result.package_label(1) == "ABC"
    assert result.assignments[1].mode == "same_section"


def test_pin_overlap_links_supplemental_table_without_order_dependency() -> None:
    known_rows = (
        ("A1", "SIG_A", "I"),
        ("A2", "SIG_B", "O"),
        ("A3", "SIG_C", "I/O"),
    )
    supplemental_rows = (
        ("A1", "SIG_A", "I"),
        ("A2", "SIG_B", "O"),
        ("A3", "SIG_C", "I/O"),
    )
    # 未知表故意排在明确表前面，证明关联不依赖逐表输出顺序。
    sources = [
        _source(0, title="Table 8-2. Connectivity Requirements"),
        _source(1, title="Table 3-1. DEF Package Pin Description"),
    ]
    targets = [
        _target(0, title=sources[0].title, rows=supplemental_rows),
        _target(1, title=sources[1].title, rows=known_rows),
    ]

    result = resolve_document_packages(
        all_tables=sources,
        target_tables=targets,
        multi_package_plans={},
    )

    assert result.package_label(0) == "DEF"
    assert result.assignments[0].mode == "pin_overlap"


def test_ambiguous_pin_overlap_does_not_guess() -> None:
    rows = (
        ("A1", "SIG_A", "I"),
        ("A2", "SIG_B", "O"),
        ("A3", "SIG_C", "I/O"),
    )
    sources = [
        _source(0, title="Table 9-1. Connectivity Requirements"),
        _source(1, title="Table 3-1. DEF Package Pins"),
        _source(2, title="Table 4-1. UVW Package Pins"),
    ]
    targets = [
        _target(0, title=sources[0].title, rows=rows),
        _target(1, title=sources[1].title, rows=rows),
        _target(2, title=sources[2].title, rows=rows),
    ]

    result = resolve_document_packages(
        all_tables=sources,
        target_tables=targets,
        multi_package_plans={},
    )

    assert result.package_label(0) == ""
    assert result.assignments[0].mode == "overlap_conflict"


def test_multi_package_plan_keeps_each_binding_separate() -> None:
    columns = (
        _Column(0, "ABC Package", "pin_no"),
        _Column(1, "XYZ Package", "pin_no"),
        _Column(2, "SIGNAL NAME", "pin_name"),
    )
    rows = (("1", "A1", "SIG_A"), ("2", "A2", "SIG_B"))
    source = _source(0, title="Table 6-1. Multi-package Pin Mapping")
    target = _target(0, title=source.title, rows=rows, columns=columns)
    plan = _Plan(
        True,
        "package_columns",
        (
            _Binding("ABC", 0, 2),
            _Binding("XYZ", 1, 2),
        ),
    )

    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[target],
        multi_package_plans={0: plan},
    )

    assignment = result.assignments[0]
    assert assignment.mode == "package_columns"
    assert len(assignment.package_keys) == 2
    assert result.registry.display_for_label("ABC") == "ABC"
    assert result.registry.display_for_label("XYZ") == "XYZ"
