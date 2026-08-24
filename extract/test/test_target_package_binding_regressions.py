"""目标表图题和物理封装绑定回归测试。"""

from __future__ import annotations

from extract.package_catalog_resolver import (
    PackageCatalogEntry,
    PackageCatalogTable,
    PackageTargetTable,
    bind_target_tables,
    consolidate_target_physical_catalog_entries,
    freeze_package_slots,
    resolve_document_package_catalog,
)
from extract.pin_package_extractor import figure_context_for_table


def catalog_table(table_id: int, title: str, headers, rows) -> PackageCatalogTable:
    return PackageCatalogTable(
        table_id=table_id,
        page_idx=0,
        title=title,
        group_context=title,
        current_chapter_titles=(title,),
        headers=tuple(headers),
        rows=tuple(tuple(row) for row in rows),
    )


def target_table(
    table_id: int,
    title: str,
    group_context: str,
    headers=("PIN", "NAME"),
) -> PackageTargetTable:
    return PackageTargetTable(
        table_id=table_id,
        page_idx=1,
        title=title,
        group_context=group_context,
        current_chapter_titles=("4 Pin Configuration and Functions",),
        headers=tuple(headers),
    )


def identity_summary_classifier(_table, _source_name, _target_tables):
    return {
        "is_package_summary": True,
        "table_role": "identity_summary",
        "header_row_index": 0,
        "columns": [
            {"column_index": 0, "role": "package_identity"},
            {"column_index": 1, "role": "package_type"},
            {"column_index": 2, "role": "package_drawing"},
            {"column_index": 3, "role": "pin_count"},
        ],
    }


def test_broad_pin_functions_uses_nearby_package_figure_title_window():
    figure_title = figure_context_for_table(
        "Pin Functions",
        [
            "4 Pin Configuration and Functions",
            "DGS Package",
            "10-Pin VSSOP",
            "(Top View)",
            "![](images/package.jpg)",
            "Not to scale",
            "Pin Functions",
        ],
    )

    assert figure_title == "DGS Package\n10-Pin VSSOP\n(Top View)"


def test_orderable_rows_with_same_physical_slot_bind_by_pin_count():
    entries = [
        PackageCatalogEntry(
            package_key="",
            identity_name="DRV8300UDPW",
            package_type="TSSOP",
            package_drawing="PW",
            pin_count="20",
        ),
        PackageCatalogEntry(
            package_key="",
            identity_name="DRV8300UDIPW",
            package_type="TSSOP",
            package_drawing="PW",
            pin_count="20",
        ),
        PackageCatalogEntry(
            package_key="",
            identity_name="DRV8300UDRGE",
            package_type="VQFN",
            package_drawing="RGE",
            pin_count="24",
        ),
    ]
    targets = [
        target_table(101, "Pin Functions—24-Pin VQFN", "Pin Functions—24-Pin VQFN"),
        target_table(102, "Pin Functions—20-Pin TSSOP", "Pin Functions—20-Pin TSSOP"),
    ]

    entries = consolidate_target_physical_catalog_entries(
        entries,
        target_tables=targets,
    )
    freeze_package_slots(entries)
    assignments = bind_target_tables(
        entries=entries,
        target_tables=targets,
        multi_package_plans={},
        multi_pkg_tab_resolution=None,
        diagnostics=[],
    )

    assert len(entries) == 2
    assert assignments[(101, 0)].pkg == "VQFN"
    assert assignments[(102, 0)].pkg == "TSSOP"


def test_target_package_figure_physical_slots_override_bad_catalog_mix():
    catalog = catalog_table(
        1,
        "Device Information",
        ["PART NUMBER", "PACKAGE", "PACKAGE DRAWING", "PINS"],
        [
            # 模拟 DRV2604L 里第二次模型/目录把 DSBGA 的 drawing 错混成 DGS。
            ["DRV2604L", "DSBGA", "DGS", "9"],
        ],
    )
    targets = [
        target_table(
            201,
            "Pin Functions",
            "Pin Functions\nYZF Package9-Pin DSBGA With 0.5-mm Pitch\n(Top View)",
        ),
        target_table(
            202,
            "Pin Functions",
            "Pin Functions\nDGS Package\n10-Pin VSSOP\n(Top View)",
        ),
    ]

    resolution = resolve_document_package_catalog(
        all_tables=[catalog],
        target_tables=targets,
        multi_package_plans={},
        use_semantic_classifier=True,
        classifier=identity_summary_classifier,
    )

    assert [
        (entry.package_type, entry.package_drawing, entry.pin_count)
        for entry in resolution.entries
    ] == [("DSBGA", "YZF", "9"), ("VSSOP", "DGS", "10")]
    assert resolution.assignment_for(201, 0).pkg == "DSBGA"
    assert resolution.assignment_for(202, 0).pkg == "VSSOP"
