"""目标表图题和物理封装绑定回归测试。"""

from __future__ import annotations

from extract.package_catalog_resolver import (
    PackageCatalogEntry,
    PackageCatalogTable,
    PackageTargetTable,
    bind_target_tables,
    consolidate_target_physical_catalog_entries,
    confirmed_package_slot_floor,
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


class DummyBinding:
    def __init__(self, package: str):
        self.package = package


class DummyPlan:
    is_multi_package = True

    def __init__(self, packages, mode="package_columns"):
        self.mode = mode
        self.bindings = tuple(DummyBinding(package) for package in packages)


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


def test_physical_consolidation_respects_confirmed_package_slot_floor():
    entries = [
        PackageCatalogEntry(
            package_key="",
            identity_name="DRV8411",
            package_type="HTSSOP",
            package_drawing="PWP",
            pin_count="16",
        ),
        PackageCatalogEntry(
            package_key="",
            identity_name="",
            package_type="HTSSOP",
            package_drawing="PWP",
            pin_count="16",
        ),
    ]
    table = target_table(
        103,
        "表 6-1. 引脚功能",
        "表 6-1. 引脚功能",
        headers=("引脚 名称", "引脚 RTE", "引脚 PWP、 DYZ", "类型(I)"),
    )
    diagnostics = []

    result = consolidate_target_physical_catalog_entries(
        entries,
        target_tables=[table],
        confirmed_slot_floor=2,
        diagnostics=diagnostics,
    )

    assert len(result) == 2
    assert diagnostics[-1]["protected_physical_signatures"] == [
        ["htssop", "pwp", "16"]
    ]


def test_physical_consolidation_still_merges_without_confirmed_floor():
    entries = [
        PackageCatalogEntry(
            package_key="",
            identity_name="DRV8411",
            package_type="HTSSOP",
            package_drawing="PWP",
            pin_count="16",
        ),
        PackageCatalogEntry(
            package_key="",
            identity_name="",
            package_type="HTSSOP",
            package_drawing="PWP",
            pin_count="16",
        ),
    ]

    result = consolidate_target_physical_catalog_entries(
        entries,
        target_tables=[],
    )

    assert len(result) == 1


def test_confirmed_package_slot_floor_excludes_package_side_labels():
    normal_table = target_table(
        104,
        "Pin Functions",
        "Pin Functions",
        headers=("NAME", "RTE PIN", "PWP DYZ PIN", "TYPE"),
    )
    side_table = target_table(
        105,
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
        headers=("BOTTOM", "TOP", "SIGNAL NAME", "TYPE"),
    )

    assert confirmed_package_slot_floor(
        target_tables=[normal_table],
        multi_package_plans={104: DummyPlan(["RTE", "PWP DYZ"])},
    ) == 2
    assert confirmed_package_slot_floor(
        target_tables=[side_table],
        multi_package_plans={105: DummyPlan(["BOTTOM", "TOP"])},
    ) == 0
    assert confirmed_package_slot_floor(
        target_tables=[normal_table],
        multi_package_plans={104: DummyPlan(["SPI", "GPIO"], mode="parallel_name_columns")},
    ) == 0


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


def test_plan_local_identity_variants_create_catalog_slots_before_binding():
    catalog = catalog_table(
        1,
        "Device Information",
        ["PART NUMBER", "PACKAGE", "PACKAGE DRAWING", "PINS"],
        [
            ["PART NUMBER", "PACKAGE", "PACKAGE DRAWING", "PINS"],
            ["DRV8350", "WQFN", "RTV", "32"],
            ["DRV8350R", "VQFN", "RGZ", "48"],
        ],
    )
    target_a = target_table(
        501,
        "Pin Functions—32-Pin DRV8350 Devices",
        "Pin Functions—32-Pin DRV8350 Devices",
        headers=("NAME", "DRV8350H PIN", "DRV8350S PIN", "TYPE"),
    )
    target_b = target_table(
        502,
        "Pin Functions—48-Pin DRV8350R Devices",
        "Pin Functions—48-Pin DRV8350R Devices",
        headers=("NAME", "DRV8350RH PIN", "DRV8350RS PIN", "TYPE"),
    )

    resolution = resolve_document_package_catalog(
        all_tables=[catalog],
        target_tables=[target_a, target_b],
        multi_package_plans={
            501: DummyPlan(["DRV8350H", "DRV8350S"]),
            502: DummyPlan(["DRV8350RH", "DRV8350RS"]),
        },
        use_semantic_classifier=True,
        classifier=identity_summary_classifier,
    )

    assert [
        entry.identity_name
        for entry in resolution.entries
    ] == ["DRV8350H", "DRV8350S", "DRV8350RH", "DRV8350RS"]
    assert resolution.assignment_for(501, 0).package_key == "slot:0"
    assert resolution.assignment_for(501, 1).package_key == "slot:1"
    assert resolution.assignment_for(502, 0).package_key == "slot:2"
    assert resolution.assignment_for(502, 1).package_key == "slot:3"


def test_multi_package_slot_shortage_is_unresolved_not_crash():
    entries = [
        PackageCatalogEntry(package_key="", package_type="FCCSP", package_drawing="CBP"),
        PackageCatalogEntry(package_key="", package_type="FCCSP", package_drawing="CBC"),
    ]
    freeze_package_slots(entries)
    table = target_table(
        301,
        "Pin Functions",
        "Pin Functions",
        headers=(
            "Signal Name",
            "Description",
            "Pkg A Pin",
            "Pkg B Pin",
            "Pkg C Pin",
        ),
    )
    diagnostics = []

    assignments = bind_target_tables(
        entries=entries,
        target_tables=[table],
        multi_package_plans={
            301: DummyPlan(
                [
                    "Pkg A",
                    "Pkg B",
                    "Pkg C",
                ]
            )
        },
        multi_pkg_tab_resolution=None,
        diagnostics=diagnostics,
    )

    assert assignments == {}
    shortage = [
        item for item in diagnostics
        if item.get("reason") == "package_catalog_slot_shortage"
    ]
    assert len(shortage) == 3
    assert {item["status"] for item in shortage} == {"unresolved"}


def test_bottom_top_headers_bind_to_same_package_from_table_title():
    entries = [
        PackageCatalogEntry(package_key="", package_type="FCCSP", package_drawing="CBP"),
        PackageCatalogEntry(package_key="", package_type="FCCSP", package_drawing="CBC"),
    ]
    freeze_package_slots(entries)
    table = target_table(
        401,
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
        headers=("BOTTOM", "TOP", "SIGNAL NAME", "TYPE"),
    )
    diagnostics = []

    assignments = bind_target_tables(
        entries=entries,
        target_tables=[table],
        multi_package_plans={401: DummyPlan(["BOTTOM", "TOP"])},
        multi_pkg_tab_resolution=None,
        diagnostics=diagnostics,
    )

    assert assignments[(401, 0)].package_key == entries[0].package_key
    assert assignments[(401, 1)].package_key == entries[0].package_key
    assert [
        item.get("effective_label")
        for item in diagnostics
        if item.get("reason") == "package_side_label"
    ] == ["CBP", "CBP"]


def test_ball_characteristics_side_labels_prefer_primary_catalog_evidence():
    entries = [
        PackageCatalogEntry(
            package_key="",
            package_type="FCBGA",
            package_drawing="CBP",
            pin_count="515",
            evidence_table_ids=[243],
        ),
        PackageCatalogEntry(
            package_key="",
            package_type="FCCSP",
            package_drawing="CBP",
            pin_count="515",
            evidence_table_ids=[244],
        ),
        PackageCatalogEntry(
            package_key="",
            package_type="FCCSP",
            package_drawing="CUS",
            pin_count="423",
            evidence_table_ids=[243],
        ),
    ]
    freeze_package_slots(entries)
    table = target_table(
        403,
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
        headers=("BALL BOTTOM [1]", "BALL TOP [1]", "PIN NAME [2]", "TYPE [4]"),
    )
    diagnostics = []

    assignments = bind_target_tables(
        entries=entries,
        target_tables=[table],
        multi_package_plans={403: DummyPlan(["BOTTOM", "TOP"])},
        multi_pkg_tab_resolution=None,
        diagnostics=diagnostics,
    )

    assert assignments[(403, 0)].package_key == entries[0].package_key
    assert assignments[(403, 1)].package_key == entries[0].package_key
    assert [
        item.get("reason")
        for item in diagnostics
        if item.get("effective_label") == "CBP"
    ] == [
        "package_side_label_disambiguated",
        "package_side_label_disambiguated",
    ]


def test_bottom_top_package_labels_bind_to_same_package():
    entries = [
        PackageCatalogEntry(package_key="", package_type="FCCSP", package_drawing="CBP"),
        PackageCatalogEntry(package_key="", package_type="FCCSP", package_drawing="CBC"),
    ]
    freeze_package_slots(entries)
    table = target_table(
        402,
        "Table 2-5. External Memory Interfaces – GPMC Signals Description",
        "Table 2-5. External Memory Interfaces – GPMC Signals Description",
        headers=(
            "Signal Name",
            "BOTTOM CBP Pkg.",
            "TOP CBP Pkg.",
            "BOTTOM CBC Pkg.",
        ),
    )
    diagnostics = []

    assignments = bind_target_tables(
        entries=entries,
        target_tables=[table],
        multi_package_plans={
            402: DummyPlan(
                [
                    "BOTTOM CBP Pkg.",
                    "TOP CBP Pkg.",
                    "BOTTOM CBC Pkg.",
                ]
            )
        },
        multi_pkg_tab_resolution=None,
        diagnostics=diagnostics,
    )

    assert assignments[(402, 0)].package_key == entries[0].package_key
    assert assignments[(402, 1)].package_key == entries[0].package_key
    assert assignments[(402, 2)].package_key == entries[1].package_key
