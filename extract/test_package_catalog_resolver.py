"""文档级封装目录和表格绑定的通用测试。"""

from unittest.mock import patch

import extract.pin_package_extractor as pin_extractor
from extract.package_catalog_resolver import (
    PackageCatalogEntry,
    PackageCatalogTable,
    PackageTargetTable,
    bind_target_tables,
    clean_package_name,
    find_package_catalog_candidates,
    freeze_package_slots,
    match_entries_in_text,
    merge_plan_package_labels,
    resolve_document_package_catalog,
)
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    TableDecision,
)
from extract.semantic_classifier import normalize_package_catalog_response


def catalog_table(table_id, title, headers, rows, page_idx=0):
    return PackageCatalogTable(
        table_id=table_id,
        page_idx=page_idx,
        title=title,
        group_context=title,
        current_chapter_titles=(title,),
        headers=tuple(headers),
        rows=tuple(tuple(row) for row in rows),
    )


def target_table(table_id, title, headers):
    return PackageTargetTable(
        table_id=table_id,
        page_idx=1,
        title=title,
        group_context=title,
        current_chapter_titles=(title,),
        headers=tuple(headers),
    )


def identity_catalog_classifier(table, source_name, target_tables):
    """测试用目录分类器：只声明身份列和物理封装列。"""

    return {
        "is_package_summary": True,
        "table_role": "identity_summary",
        "header_row_index": 0,
        "columns": [
            {"column_index": 0, "role": "package_identity"},
            {"column_index": 1, "role": "package_type"},
        ],
    }


def batch_identity_catalog_classifier(tables, source_name, target_tables):
    """适配当前四表一批接口，供完整提取流程测试替换真实模型。"""

    return {
        str(table_id): identity_catalog_classifier(
            table,
            source_name,
            target_tables,
        )
        for table_id, table in tables
    }


def test_summary_locator_uses_context_headers_and_document_edge():
    summary = catalog_table(
        0,
        "Device Information",
        ["PART NUMBER", "PACKAGE", "BODY SIZE"],
        [["INA290", "SC70", "2.0 x 2.1"]],
    )
    timing = catalog_table(
        1,
        "Timing Requirements",
        ["PARAMETER", "MIN", "MAX"],
        [["Clock", "1", "2"]],
        page_idx=10,
    )

    assert summary in find_package_catalog_candidates([summary, timing])


def test_summary_locator_without_page_numbers_only_uses_own_priority_title():
    tables = [
        catalog_table(
            0,
            "Package Information",
            ["DEVICE", "PACKAGE"],
            [["DEV100", "QFN 32"]],
            page_idx=None,
        )
    ]
    tables.extend(
        catalog_table(
            table_id,
            f"Electrical Table {table_id}",
            ["PARAMETER", "MIN", "MAX"],
            [["Clock", "1", "2"]],
            page_idx=None,
        )
        for table_id in range(1, 20)
    )

    candidates = find_package_catalog_candidates(tables)
    assert tables[0] in candidates
    assert tables[len(tables) // 2] not in candidates


def test_unknown_table_without_page_metadata_is_not_guessed_by_table_order():
    unknown_summary = catalog_table(
        0,
        "Overview",
        ["IDENTIFIER", "OPTION"],
        [["DEV100", "QFN 32"]],
        page_idx=None,
    )
    middle_tables = [
        catalog_table(
            table_id,
            f"Electrical Table {table_id}",
            ["PARAMETER", "MIN", "MAX"],
            [["Clock", "1", "2"]],
            page_idx=None,
        )
        for table_id in range(1, 20)
    ]

    candidates = find_package_catalog_candidates(
        [unknown_summary, *middle_tables]
    )

    assert unknown_summary not in candidates


def test_catalog_candidates_follow_toc_page_windows_and_exclude_pin_tables():
    tables = [
        catalog_table(
            table_id=page,
            title=f"Table {page}. Any Data",
            headers=["A", "B"],
            rows=[["x", "y"]],
            page_idx=page,
        )
        for page in (0, 1, 2, 3, 4, 5, 6, 7, 8, 50, 90, 99)
    ]

    candidates = find_package_catalog_candidates(
        tables,
        document_page_count=100,
        toc_page_range=(2, 4),
        excluded_table_ids={6},
    )

    assert [table.page_idx for table in candidates] == [0, 1, 5, 7, 90, 99]


def test_catalog_candidates_without_toc_use_first_and_last_ten_pages():
    tables = [
        catalog_table(
            table_id=page,
            title=f"Table {page}. Any Data",
            headers=["A", "B"],
            rows=[["x", "y"]],
            page_idx=page,
        )
        for page in (0, 9, 10, 49, 50, 89, 90, 99)
    ]

    candidates = find_package_catalog_candidates(
        tables,
        document_page_count=100,
    )

    assert [table.page_idx for table in candidates] == [0, 9, 90, 99]


def test_priority_title_checks_own_table_title_not_inherited_chapter():
    inherited_only = PackageCatalogTable(
        table_id=1,
        page_idx=50,
        title="Table 20. Electrical Characteristics",
        group_context="2 Device Information\nTable 20. Electrical Characteristics",
        current_chapter_titles=("2 Device Information",),
        headers=("PARAMETER", "VALUE"),
        rows=(("Clock", "10"),),
    )
    own_title = catalog_table(
        2,
        "Table 21. Device Information",
        ["DEVICE", "PACKAGE"],
        [["DEV100", "QFN"]],
        page_idx=50,
    )

    candidates = find_package_catalog_candidates(
        [inherited_only, own_title],
        document_page_count=100,
        toc_page_range=(2, 4),
    )

    assert inherited_only not in candidates
    assert own_title in candidates


def test_catalog_filters_unrelated_summary_devices_by_target_evidence():
    summary = catalog_table(
        0,
        "Device Comparison",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["VSC8224", "BGA"],
            ["VSC8234", "BGA"],
            ["VSC8244", "BGA"],
        ],
    )
    target = target_table(
        1,
        "VSC8234 HSBGA Ball Descriptions",
        ["HSBGA BALL", "SIGNAL NAME", "TYPE"],
    )

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "identity_summary",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "package_identity"},
                {"column_index": 1, "role": "package_type"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assignment = result.assignment_for(1, 0)
    assert assignment.pkg == "BGA"
    assert assignment.reason == "table_title_or_header"


def test_verified_package_type_can_bind_a_target_title():
    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["DEV100", "QFN 32"],
            ["DEV200", "BGA 64"],
        ],
    )
    target = target_table(
        1,
        "QFN 32 Pin Functions",
        ["PIN NO", "PIN NAME", "TYPE"],
    )

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "identity_summary",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "package_identity"},
                {"column_index": 1, "role": "package_type"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert result.assignment_for(1, 0).pkg == "QFN 32"
    assert result.assignment_for(1, 0).reason == "table_title_or_header"


def test_multi_package_columns_bind_each_real_package_name():
    target = target_table(
        2,
        "Pin Descriptions",
        ["PIN NAME", "SSOP 28 PIN", "QFN 28 PIN", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("SSOP 28", 1, 0, 3),
            PackageBinding("QFN 28", 2, 0, 3),
        ),
    )

    result = resolve_document_package_catalog(
        all_tables=[],
        target_tables=[target],
        multi_package_plans={2: plan},
    )

    assert result.assignment_for(2, 0).pkg == "SSOP 28"
    assert result.assignment_for(2, 1).pkg == "QFN 28"


def test_multi_package_type_labels_do_not_duplicate_catalog_entries():
    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["DEV100", "SSOP 28"],
            ["DEV200", "QFN 28"],
        ],
    )
    target = target_table(
        2,
        "Pin Descriptions",
        ["PIN NAME", "SSOP 28 PIN", "QFN 28 PIN", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("SSOP 28", 1, 0, 3),
            PackageBinding("QFN 28", 2, 0, 3),
        ),
    )

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "identity_summary",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "package_identity"},
                {"column_index": 1, "role": "package_type"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={2: plan},
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [entry.identity_name for entry in result.entries] == [
        "DEV100",
        "DEV200",
    ]
    assert result.assignment_for(2, 0).pkg == "SSOP 28"
    assert result.assignment_for(2, 1).pkg == "QFN 28"


def test_confirmed_plan_removes_identity_only_extra_slots():
    """严格两分支表存在时，四条纯器件型号不能创建四个 pkg。"""

    target = target_table(
        2,
        "Pin Attributes",
        ["SIGNAL NAME", "PKG-C NO.", "PKG-S NO.", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("PKG-C", 1, 0, 3),
            PackageBinding("PKG-S", 2, 0, 3),
        ),
    )
    diagnostics = []
    entries = [
        PackageCatalogEntry("", identity_name=f"DEVICE-{index}")
        for index in range(4)
    ]

    result = merge_plan_package_labels(
        entries,
        target_tables=[target],
        multi_package_plans={2: plan},
        diagnostics=diagnostics,
    )

    assert len(result) == 2
    assert all(not entry.identity_name for entry in result)
    assert diagnostics[-1]["stage"] == (
        "package_catalog_confirmed_plan_reconciliation"
    )
    assert diagnostics[-1]["before"] == 4
    assert diagnostics[-1]["after"] == 2


def test_confirmed_plan_keeps_physical_packages_and_drops_weak_identities():
    """两个真实物理封装加两个器件型号时，最终仍只有两个槽位。"""

    target = target_table(
        2,
        "Pin Attributes",
        ["SIGNAL NAME", "FCBGA NO.", "FCCSP NO.", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("FCBGA", 1, 0, 3),
            PackageBinding("FCCSP", 2, 0, 3),
        ),
    )
    entries = [
        PackageCatalogEntry(
            "",
            identity_name="DEVICE-A",
            package_type="FCBGA",
            package_drawing="ALV",
            pin_count="441",
        ),
        PackageCatalogEntry(
            "",
            identity_name="DEVICE-B",
            package_type="FCCSP",
            package_drawing="S",
            pin_count="293",
        ),
        PackageCatalogEntry("", identity_name="DEVICE-S"),
        PackageCatalogEntry("", identity_name="DEVICE-FAMILY"),
    ]

    result = merge_plan_package_labels(
        entries,
        target_tables=[target],
        multi_package_plans={2: plan},
    )

    assert len(result) == 2
    assert [entry.package_type for entry in result] == ["FCBGA", "FCCSP"]


def test_confirmed_plan_does_not_merge_same_physical_name_mapping_spaces():
    """相同封装元数据但不同器件映射空间仍是两个独立槽位。"""

    target = target_table(
        2,
        "Pin Attributes",
        ["SIGNAL NAME", "DEVICE-A NO.", "DEVICE-B NO.", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("DEVICE-A", 1, 0, 3),
            PackageBinding("DEVICE-B", 2, 0, 3),
        ),
    )
    entries = [
        PackageCatalogEntry(
            "",
            identity_name="DEVICE-A",
            package_type="BGA",
            package_drawing="ZCZ",
            pin_count="324",
        ),
        PackageCatalogEntry(
            "",
            identity_name="DEVICE-B",
            package_type="BGA",
            package_drawing="ZCZ",
            pin_count="324",
        ),
        PackageCatalogEntry("", identity_name="DEVICE-FAMILY"),
    ]

    result = merge_plan_package_labels(
        entries,
        target_tables=[target],
        multi_package_plans={2: plan},
    )

    assert len(result) == 2
    assert [entry.identity_name for entry in result] == [
        "DEVICE-A",
        "DEVICE-B",
    ]


def test_confirmed_plan_does_not_delete_additional_physical_packages():
    """真实物理签名多于局部表分支时，不能用一张表裁剪全文目录。"""

    target = target_table(
        2,
        "Partial Pin Map",
        ["SIGNAL NAME", "PKG-A NO.", "PKG-B NO.", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("PKG-A", 1, 0, 3),
            PackageBinding("PKG-B", 2, 0, 3),
        ),
    )
    entries = [
        PackageCatalogEntry(
            "",
            package_type=f"PACKAGE-{index}",
            package_drawing=f"DRAWING-{index}",
            pin_count=str(20 + index),
        )
        for index in range(3)
    ]

    result = merge_plan_package_labels(
        entries,
        target_tables=[target],
        multi_package_plans={2: plan},
    )

    assert len(result) == 3


def test_name_branch_plan_does_not_trim_document_catalog():
    """名称分支可能只覆盖部分封装，不能用于裁剪全文目录。"""

    target = target_table(
        2,
        "Mode-specific Pin Names",
        ["PACKAGE-A NAME", "PACKAGE-B NAME", "PIN NO.", "TYPE"],
    )
    plan = MultiPackagePlan(
        True,
        "package_name_columns",
        bindings=(
            PackageBinding("PACKAGE-A", 2, 0, 3),
            PackageBinding("PACKAGE-B", 2, 1, 3),
        ),
    )
    entries = [
        PackageCatalogEntry("", identity_name=f"DEVICE-{index}")
        for index in range(3)
    ]

    result = merge_plan_package_labels(
        entries,
        target_tables=[target],
        multi_package_plans={2: plan},
    )

    assert len(result) == 3


def test_identity_summary_creates_real_packages_and_packaging_only_enriches():
    """总述表创建 INA 身份，包装表只能补 DCK/DGK/RGV 等元数据。"""

    identity_summary = catalog_table(
        0,
        "器件信息",
        ["器件型号", "封装", "封装尺寸"],
        [
            ["器件型号", "封装", "封装尺寸"],
            ["INA290", "SC-70 (5)", "2.0 x 2.1"],
            ["INA2290", "VSSOP (8)", "3.0 x 3.0"],
            ["INA4290", "QFN (16)", "3.0 x 3.0"],
        ],
    )
    packaging = catalog_table(
        15,
        "Packaging Information",
        ["Orderable Device", "Package Type", "Package Drawing", "Pins"],
        [
            ["Orderable Device", "Package Type", "Package Drawing", "Pins"],
            ["INA290A1IDCKR", "SC-70", "DCK", "5"],
            ["INA2290A1IDGKR", "VSSOP", "DGK", "8"],
            ["INA4290A1IRGVR", "VQFN", "RGV", "16"],
        ],
        page_idx=30,
    )
    targets = [
        target_table(1, "Table 5-1. Pin Functions: INA290", ["PIN", "NAME"]),
        target_table(2, "Table 5-2. Pin Functions: INA2290", ["PIN", "NAME"]),
        target_table(3, "Table 5-3. Pin Functions: INA4290", ["PIN", "NAME"]),
    ]

    def classifier(table, source_name, target_tables):
        if table.table_id == 0:
            return {
                "is_package_summary": True,
                "table_role": "identity_summary",
                "header_row_index": 0,
                "columns": [
                    {"column_index": 0, "role": "package_identity"},
                    {"column_index": 1, "role": "package_type"},
                ],
            }
        return {
            "is_package_summary": True,
            "table_role": "packaging_metadata",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "orderable_sku"},
                {"column_index": 1, "role": "package_type"},
                {"column_index": 2, "role": "package_drawing"},
                {"column_index": 3, "role": "pin_count"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[identity_summary, packaging],
        target_tables=targets,
        multi_package_plans={
            target.table_id: MultiPackagePlan(False, "single_package")
            for target in targets
        },
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [entry.identity_name for entry in result.entries] == [
        "INA290",
        "INA2290",
        "INA4290",
    ]
    assert [
        (entry.package_type, entry.package_drawing, entry.pin_count)
        for entry in result.entries
    ] == [
        ("SC-70", "DCK", "5"),
        ("VSSOP", "DGK", "8"),
        ("QFN", "RGV", "16"),
    ]
    assert [result.assignment_for(table_id, 0).pkg for table_id in (1, 2, 3)] == [
        "SC-70",
        "VSSOP",
        "QFN",
    ]


def test_same_public_package_type_does_not_merge_independent_identity_slots():
    """两个独立型号即使同为 QFN，也必须保持两个物理映射槽位。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE", "DRAWING", "PINS"],
        [
            ["DEVICE", "PACKAGE", "DRAWING", "PINS"],
            ["DEV-A", "QFN", "RGV", "16"],
            ["DEV-B", "QFN", "RGT", "16"],
        ],
    )

    def classifier(table, source_name, target_tables):
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

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[],
        multi_package_plans={},
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [entry.package_key for entry in result.entries] == [
        "slot:0",
        "slot:1",
    ]
    assert [item.pkg for item in result.declared_assignments()] == [
        "QFN",
        "QFN",
    ]


def test_packaging_metadata_can_establish_physical_slot_without_identity():
    """没有身份总述时，完整物理元数据可以确定槽位，但 SKU 不能成为 pkg。"""

    packaging = catalog_table(
        0,
        "Packaging Information",
        ["Orderable Device", "Package Type", "Package Drawing", "Pins"],
        [
            ["Orderable Device", "Package Type", "Package Drawing", "Pins"],
            ["INA2290A1IDGKR", "VSSOP", "DGK", "8"],
        ],
    )

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "packaging_metadata",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "orderable_sku"},
                {"column_index": 1, "role": "package_type"},
                {"column_index": 2, "role": "package_drawing"},
                {"column_index": 3, "role": "pin_count"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[packaging],
        target_tables=[],
        multi_package_plans={},
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert len(result.entries) == 1
    assert result.entries[0].identity_name == ""
    assert result.entries[0].package_type == "VSSOP"
    assert result.declared_assignments()[0].pkg == "VSSOP"


def test_catalog_header_repair_restores_first_data_row():
    """模型把第一条数据行当表头时，应回退到真实标准表头。"""

    packaging = catalog_table(
        0,
        "Packaging Information",
        ["Device", "Package Name", "Package Type", "Pins"],
        [
            ["Device", "Package Name", "Package Type", "Pins"],
            ["CC1110F16RHHR", "RHH", "VQFN", "36"],
            ["CC1111F32RSP", "RSP", "VQFNP", "36"],
        ],
    )

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "packaging_metadata",
            # 模拟 DeepSeek 把第一条数据行误报为 header。
            "header_row_index": 1,
            "columns": [
                {"column_index": 0, "role": "orderable_sku"},
                {"column_index": 1, "role": "package_drawing"},
                {"column_index": 2, "role": "package_type"},
                {"column_index": 3, "role": "pin_count"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[packaging],
        target_tables=[],
        multi_package_plans={},
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [
        (entry.package_type, entry.package_drawing, entry.pin_count)
        for entry in result.entries
    ] == [
        ("VQFN", "RHH", "36"),
        ("VQFNP", "RSP", "36"),
    ]
    accepted = next(
        item
        for item in result.diagnostics
        if item.get("stage") == "package_catalog"
        and item.get("status") == "structure_accepted"
    )
    assert accepted["header_row_index"] == 0


def test_standard_catalog_fallback_and_family_wildcard_bind_cc430_tables():
    """模型无返回时，标准 TI 表兜底；标题 CC430F614x 可绑定具体型号槽位。"""

    device_info = catalog_table(
        0,
        "器件信息",
        ["器件型号", "封装", "封装尺寸"],
        [
            ["器件型号", "封装", "封装尺寸"],
            ["CC430F6147IRGC", "VQFN (64)", "9mm x 9mm"],
            ["CC430F5147IRGZ", "VQFN (48)", "7mm x 7mm"],
        ],
    )
    packaging = catalog_table(
        10,
        "PACKAGING INFORMATION",
        ["Orderable Device", "Status", "Package Type", "Package Drawing", "Pins"],
        [
            ["Orderable Device", "Status", "Package Type", "Package Drawing", "Pins"],
            ["CC430F5147IRGZR", "ACTIVE", "VQFN", "RGZ", "48"],
            ["CC430F6147IRGCR", "ACTIVE", "VQFN", "RGC", "64"],
        ],
    )
    f614 = target_table(
        2,
        "Table 4-1. CC430F614x Terminal Functions",
        ["TERMINAL NAME", "TERMINAL NO.", "I/O", "DESCRIPTION"],
    )
    f514 = target_table(
        4,
        "Table 4-2. CC430F514x and CC430F512x Terminal Functions",
        ["TERMINAL NAME", "TERMINAL NO.", "I/O", "DESCRIPTION"],
    )

    def classifier(table, source_name, target_tables):
        raise RuntimeError("DeepSeek returned empty message content")

    result = resolve_document_package_catalog(
        all_tables=[device_info, packaging],
        target_tables=[f614, f514],
        multi_package_plans={
            2: MultiPackagePlan(False, "single_package"),
            4: MultiPackagePlan(False, "single_package"),
        },
        source_name="CC430F514x_CC430F614x_64_48",
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [
        (entry.identity_name, entry.package_drawing, entry.pin_count)
        for entry in result.entries
    ] == [
        ("CC430F6147IRGC", "RGC", "64"),
        ("CC430F5147IRGZ", "RGZ", "48"),
    ]
    assert result.assignment_for(2, 0).package_key == "slot:0"
    assert result.assignment_for(4, 0).package_key == "slot:1"
    assert result.assignment_for(2, 0).reason == "cross_table_package_branch"
    assert any(
        item.get("stage") == "package_catalog_standard_fallback"
        and item.get("status") == "applied"
        for item in result.diagnostics
    )


def test_confirmed_cross_table_drawing_becomes_public_pkg_label():
    """同一物理封装族的跨表分支应输出已确认 drawing，而不是 VQFN1/VQFN2。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE", "DRAWING", "PINS"],
        [
            ["DEVICE", "PACKAGE", "DRAWING", "PINS"],
            ["DEV-RGZ", "VQFN", "RGZ", "48"],
            ["DEV-RHB", "VQFN", "RHB", "32"],
        ],
    )
    targets = [
        target_table(1, "RGZ Package Pin Functions", ["PIN NO", "PIN NAME"]),
        target_table(2, "RHB Package Pin Functions", ["PIN NO", "PIN NAME"]),
    ]

    def classifier(table, source_name, target_tables):
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

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=targets,
        multi_package_plans={
            target.table_id: MultiPackagePlan(False, "single_package")
            for target in targets
        },
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [item.pkg for item in result.declared_assignments()] == ["RGZ", "RHB"]
    assert result.assignment_for(1, 0).pkg == "RGZ"
    assert result.assignment_for(2, 0).pkg == "RHB"
    assert [entry.public_label for entry in result.entries] == ["RGZ", "RHB"]


def test_title_symbol_package_must_link_reverses_qfn_column_binding():
    """表题 RKP(QFN40)/RGE(QFN24) 必须覆盖本地 QFN24/QFN40 顺序。"""

    entries = [
        PackageCatalogEntry("", identity_aliases=["RKP"], public_label="RKP"),
        PackageCatalogEntry("", identity_aliases=["RGE"], public_label="RGE"),
    ]
    freeze_package_slots(entries)
    table = target_table(
        7,
        "Table 7-5. RKP (QFN40) and RGE (QFN24) Pin Functions",
        ["PIN NAME", "QFN24 PIN NO", "QFN40 PIN NO"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("QFN24", 1, 0, None),
            PackageBinding("QFN40", 2, 0, None),
        ),
    )
    diagnostics = []

    assignments = bind_target_tables(
        entries=entries,
        target_tables=[table],
        multi_package_plans={7: plan},
        diagnostics=diagnostics,
    )

    assert assignments[(7, 0)].pkg == "RGE"
    assert assignments[(7, 1)].pkg == "RKP"
    resolved = [
        item
        for item in diagnostics
        if item.get("stage") == "package_binding"
    ]
    assert [item.get("effective_label") for item in resolved] == ["RGE", "RKP"]
    assert resolved[0]["symbol_package_link_sources"] == ["table_title"]


def test_header_symbol_package_must_link_reverses_qfn_column_binding():
    """表头中的 Symbol(Package) 与表题同权，可修正本地列标签。"""

    entries = [
        PackageCatalogEntry("", identity_aliases=["RKP"], public_label="RKP"),
        PackageCatalogEntry("", identity_aliases=["RGE"], public_label="RGE"),
    ]
    freeze_package_slots(entries)
    table = target_table(
        8,
        "Pin Functions",
        ["PIN NAME", "RGE (QFN24) PIN NO", "RKP (QFN40) PIN NO"],
    )
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("QFN24", 1, 0, None),
            PackageBinding("QFN40", 2, 0, None),
        ),
    )
    diagnostics = []

    assignments = bind_target_tables(
        entries=entries,
        target_tables=[table],
        multi_package_plans={8: plan},
        diagnostics=diagnostics,
    )

    assert assignments[(8, 0)].pkg == "RGE"
    assert assignments[(8, 1)].pkg == "RKP"
    resolved = [
        item
        for item in diagnostics
        if item.get("stage") == "package_binding"
    ]
    assert [item.get("effective_label") for item in resolved] == ["RGE", "RKP"]
    assert resolved[0]["symbol_package_link_sources"] == ["table_header"]


def test_unresolved_tables_share_one_frozen_fallback_slot():
    targets = [
        target_table(3, "Pin Functions A", ["PIN NO", "PIN NAME"]),
        target_table(4, "Pin Functions B", ["PIN NO", "PIN NAME"]),
        target_table(5, "Pin Functions C", ["PIN NO", "PIN NAME"]),
    ]
    result = resolve_document_package_catalog(
        all_tables=[],
        target_tables=targets,
        multi_package_plans={
            target.table_id: MultiPackagePlan(False, "single_package")
            for target in targets
        },
    )

    assert len(result.entries) == 1
    assert {
        result.assignment_for(table.table_id, 0).package_key
        for table in targets
    } == {"slot:0"}
    assert [item.pkg for item in result.declared_assignments()] == ["a"]


def test_package_name_rejects_multiple_or_overlong_values():
    assert clean_package_name("SF2507") == "SF2507"
    assert clean_package_name("SF2507|SF2507E") == ""
    assert clean_package_name("ABCDEFGHIJKLMNOP") == ""


def test_catalog_model_response_keeps_only_structure_not_package_values():
    normalized = normalize_package_catalog_response(
        {
            "is_package_summary": True,
            "table_role": "identity_summary",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "package_identity"},
                {"column_index": 1, "role": "package_type"},
            ],
            # 即使模型违规返回名称，规范化层也必须彻底丢弃。
            "packages": [{"name": "WRONG_MODEL_VALUE"}],
        }
    )

    assert normalized == {
        "is_package_summary": True,
        "table_role": "identity_summary",
        "header_row_index": 0,
        "columns": [
            {"column_index": 0, "role": "package_identity"},
            {"column_index": 1, "role": "package_type"},
        ],
    }


def test_full_pipeline_uses_catalog_name_without_changing_pin_mapping():
    summary = TableCandidate(
        html=(
            "<table>"
            "<tr><td>DEVICE</td><td>PACKAGE</td></tr>"
            "<tr><td>DEV100</td><td>QFN 32</td></tr>"
            "</table>"
        ),
        page_idx=0,
        title="Device Information",
        group_context="Device Information",
        current_chapter_titles=("Device Information",),
    )
    pin_table = TableCandidate(
        html=(
            "<table>"
            "<tr><td>PIN NO.</td><td>PIN NAME</td><td>TYPE</td></tr>"
            "<tr><td>1</td><td>VDD</td><td>P</td></tr>"
            "</table>"
        ),
        page_idx=1,
        title="DEV100 Pin Functions",
        group_context="DEV100 Pin Functions",
        current_chapter_titles=("DEV100 Pin Functions",),
    )
    columns = [
        ColumnDecision(0, "PIN NO.", "pin_no"),
        ColumnDecision(1, "PIN NAME", "pin_name"),
        ColumnDecision(2, "TYPE", "type"),
    ]

    def package_classifier(table, source_name, target_tables):
        if table.title != "Device Information":
            return {
                "is_package_summary": False,
                "table_role": "irrelevant",
                "header_row_index": 0,
                "columns": [],
            }
        return {
            "is_package_summary": True,
            "table_role": "identity_summary",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "package_identity"},
                {"column_index": 1, "role": "package_type"},
            ],
        }

    with (
        patch.object(
            pin_extractor,
            "decide_all_tables",
            return_value={1: TableDecision(True, columns=columns)},
        ),
        patch(
            "extract.semantic_classifier.classify_package_catalog_tables",
            side_effect=lambda tables, source_name, target_tables: {
                str(table_id): package_classifier(
                    table,
                    source_name,
                    target_tables,
                )
                for table_id, table in tables
            },
        ),
    ):
        result = pin_extractor.extract_pin_package_info_from_table_candidates(
            [summary, pin_table],
            source_name="generic-document",
            use_semantic_classifier=True,
        )

    assert len(result) == 1
    assert result[0]["pkg"] == "QFN 32"
    assert result[0]["group_list"][0]["pin_list"] == [
        {"pin_no": "1", "pin_name": "VDD", "type": "P"}
    ]


def test_single_package_without_local_evidence_binds_the_only_slot():
    """单封装文档无需每张表重复写封装名，也能绑定唯一槽位。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [["DEVICE", "PACKAGE"], ["DEV-A", "QFN 32"]],
    )
    target = target_table(
        1,
        "GPIO Functions",
        ["PIN NO", "PIN NAME", "TYPE"],
    )
    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        use_semantic_classifier=True,
        classifier=identity_catalog_classifier,
    )

    assignment = result.assignment_for(1, 0)
    assert assignment is not None
    assert assignment.pkg == "QFN 32"
    assert assignment.reason == "single_document_package"


def test_all_unresolved_catalog_slots_fall_back_to_single_package():
    """多个目录槽位全都无法绑定时，按单封装兜底避免 0 输出。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["DEV-A", "QFN 32"],
            ["DEV-B", "BGA 64"],
        ],
    )
    target = target_table(
        1,
        "GPIO Functions",
        ["PIN NO", "PIN NAME", "TYPE"],
    )
    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        use_semantic_classifier=True,
        classifier=identity_catalog_classifier,
    )

    assert len(result.entries) == 1
    assert result.assignment_for(1, 0).pkg == "QFN 32"
    assert result.assignment_for(1, 0).reason == "single_document_package"
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.get("stage") == "single_package_all_unresolved_fallback"
    )
    assert diagnostic["before"] == 2
    assert diagnostic["after"] == 1
    assert diagnostic["document_packages_before"] == ["QFN 32", "BGA 64"]


def test_multi_count_source_name_blocks_all_unresolved_single_package_fallback():
    """文件名有多个 pin_count 时，不能把全 unresolved 多目标任务压成单封装。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["DEV-A", "QFN 36"],
            ["DEV-B", "BGA 36"],
        ],
    )
    target = target_table(
        1,
        "Pin Functions",
        ["PIN NO", "PIN NAME", "TYPE"],
    )
    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        source_name="CC1110Fx CC1111Fx_36_36",
        use_semantic_classifier=True,
        classifier=identity_catalog_classifier,
    )

    assert len(result.entries) == 2
    assert result.assignment_for(1, 0) is None
    assert not any(
        item.get("stage") == "single_package_all_unresolved_fallback"
        for item in result.diagnostics
    )


def test_multi_count_source_without_catalog_does_not_create_anonymous_slot():
    """多目标文件 catalog 全空时，不再创建会吞并全部表的匿名单槽位。"""

    target = target_table(
        1,
        "Pin Functions",
        ["PIN NO", "PIN NAME", "TYPE"],
    )
    result = resolve_document_package_catalog(
        all_tables=[],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        source_name="CC430F514x_CC430F614x_64_48",
    )

    assert result.entries == []
    assert result.assignment_for(1, 0) is None
    assert any(
        item.get("stage") == "package_catalog_anonymous_slot_guard"
        and item.get("status") == "blocked"
        for item in result.diagnostics
    )


def test_all_unresolved_single_package_fallback_prefers_source_name_hint():
    """单封装文件名中的 ZWT_361 可以从 ZCE/ZWT 目录中选择目标槽位。"""

    summary = catalog_table(
        0,
        "Packaging Information",
        ["DEVICE", "PACKAGE", "DRAWING", "PINS"],
        [
            ["DEVICE", "PACKAGE", "DRAWING", "PINS"],
            ["AM1802EZCED3", "NFBGA", "ZCE", "361"],
            ["AM1802EZWTD3", "NFBGA", "ZWT", "361"],
        ],
    )
    target = target_table(
        1,
        "Table 3-3. Reset and JTAG Terminal Functions",
        ["TERMINAL NAME", "NO.", "TYPE"],
    )

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "packaging_metadata",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "orderable_sku"},
                {"column_index": 1, "role": "package_type"},
                {"column_index": 2, "role": "package_drawing"},
                {"column_index": 3, "role": "pin_count"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        source_name="am1802-ZWT_361",
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert len(result.entries) == 1
    assert result.entries[0].package_drawing == "ZWT"
    assert result.entries[0].public_label == "ZWT"
    assert result.assignment_for(1, 0).pkg == "ZWT"


def test_multi_package_ambiguous_evidence_stays_unresolved():
    """局部证据同时命中两个槽位时，不能按目录顺序选择第一个。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["DEV-A", "BGA 64"],
            ["DEV-B", "BGA 100"],
        ],
    )
    target = target_table(
        1,
        "DEV-A and DEV-B Pin Functions",
        ["BALL", "SIGNAL NAME", "TYPE"],
    )
    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: MultiPackagePlan(False, "single_package")},
        use_semantic_classifier=True,
        classifier=identity_catalog_classifier,
    )

    assert result.assignment_for(1, 0) is None
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.get("stage") == "package_binding" and item.get("table_id") == 1
    )
    assert diagnostic["reason"] == "ambiguous_package_evidence"
    assert diagnostic["matched_packages"] == ["BGA 64", "BGA 100"]


def test_package_context_pin_count_filters_generic_package_matches():
    """VQFN (20) 这类图题上下文应使用 pin_count 消除 VQFN 泛化歧义。"""

    entries = [
        PackageCatalogEntry(
            package_key="slot:0",
            identity_name="DRV8242-Q1",
            package_type="VQFN",
            pin_count="20",
        ),
        PackageCatalogEntry(
            package_key="slot:1",
            identity_name="DRV8243-Q1",
            package_type="VQFN",
            pin_count="14",
        ),
        PackageCatalogEntry(
            package_key="slot:2",
            identity_name="DRV8244-Q1",
            package_type="VQFN",
            pin_count="16",
        ),
    ]

    matches = match_entries_in_text(
        entries,
        "Figure 6-1. DRV8242H-Q1 HW variant in VQFN (20) package",
    )

    assert [entry.package_key for entry in matches] == ["slot:0"]


def test_package_context_parenthesized_suffix_pin_count_filters_matches():
    """VQFN-HR (14) 和 HVSSOP (28) 也应被识别成明确封装 pin_count。"""

    entries = [
        PackageCatalogEntry(
            package_key="slot:0",
            identity_name="DRV8143-Q1",
            package_type="VQFN",
            pin_count="14",
        ),
        PackageCatalogEntry(
            package_key="slot:1",
            identity_name="DRV8144-Q1",
            package_type="VQFN",
            pin_count="16",
        ),
        PackageCatalogEntry(
            package_key="slot:2",
            identity_name="DRV8143P-Q1",
            package_type="HVSSOP",
            pin_count="28",
        ),
    ]

    vqfn_matches = match_entries_in_text(
        entries,
        "图 6-1. 采用 VQFN-HR (14) 封装的 DRV8143H-Q1 HW 型号",
    )
    hvssop_matches = match_entries_in_text(
        entries,
        "图 6-2. 采用 HVSSOP (28)封装的 DRV8143P-Q1 SPI (P)型号",
    )

    assert [entry.package_key for entry in vqfn_matches] == ["slot:0"]
    assert [entry.package_key for entry in hvssop_matches] == ["slot:2"]


def test_target_figure_variant_identities_create_independent_slots():
    """图题里的 H/S/P 变体应从 family 目录派生为独立槽位。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE", "PINS"],
        [
            ["DEVICE", "PACKAGE", "PINS"],
            ["DRV8242-Q1", "VQFN", "20"],
        ],
    )
    targets = [
        PackageTargetTable(
            table_id=1,
            page_idx=1,
            title="Table 6-1. Pin Functions",
            group_context="Table 6-1. Pin Functions\nFigure 6-1. DRV8242H-Q1 VQFN (20)",
            current_chapter_titles=("Pin Functions",),
            headers=("PIN", "NAME"),
        ),
        PackageTargetTable(
            table_id=2,
            page_idx=2,
            title="Table 6-2. Pin Functions",
            group_context="Table 6-2. Pin Functions\nFigure 6-2. DRV8242S-Q1 VQFN (20)",
            current_chapter_titles=("Pin Functions",),
            headers=("PIN", "NAME"),
        ),
        PackageTargetTable(
            table_id=3,
            page_idx=3,
            title="Table 6-3. Pin Functions",
            group_context="Table 6-3. Pin Functions\nFigure 6-3. DRV8242P-Q1 VQFN (20)",
            current_chapter_titles=("Pin Functions",),
            headers=("PIN", "NAME"),
        ),
    ]

    def classifier(table, source_name, target_tables):
        return {
            "is_package_summary": True,
            "table_role": "identity_summary",
            "header_row_index": 0,
            "columns": [
                {"column_index": 0, "role": "package_identity"},
                {"column_index": 1, "role": "package_type"},
                {"column_index": 2, "role": "pin_count"},
            ],
        }

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=targets,
        multi_package_plans={
            target.table_id: MultiPackagePlan(False, "single_package")
            for target in targets
        },
        source_name="DRV8242-Q1_20_20_20.pdf",
        use_semantic_classifier=True,
        classifier=classifier,
    )

    assert [entry.identity_name for entry in result.entries] == [
        "DRV8242H-Q1",
        "DRV8242S-Q1",
        "DRV8242P-Q1",
    ]
    assert [(entry.package_type, entry.pin_count) for entry in result.entries] == [
        ("VQFN", "20"),
        ("VQFN", "20"),
        ("VQFN", "20"),
    ]
    assert [
        result.assignment_for(table.table_id, 0).package_key for table in targets
    ] == ["slot:0", "slot:1", "slot:2"]


def test_target_figure_variant_supports_spaced_suffix_identity():
    """PDF 中的 ``DRV8143P -Q1`` 应规范成 ``DRV8143P-Q1``。"""

    targets = [
        PackageTargetTable(
            table_id=1,
            page_idx=1,
            title="表 6-1. 引脚功能",
            group_context="表 6-1. 引脚功能\n图 6-1. 采用 VQFN-HR (14) 封装的 DRV8143H-Q1 HW 型号",
            current_chapter_titles=("引脚功能",),
            headers=("引脚", "名称"),
        ),
        PackageTargetTable(
            table_id=2,
            page_idx=2,
            title="表 6-2. 引脚功能",
            group_context="表 6-2. 引脚功能\n图 6-2. 采用 HVSSOP (28)封装的 DRV8143P -Q1 SPI (P)型号",
            current_chapter_titles=("引脚功能",),
            headers=("引脚", "名称"),
        ),
        PackageTargetTable(
            table_id=3,
            page_idx=3,
            title="表 6-3. 引脚功能",
            group_context="表 6-3. 引脚功能\n图 6-3. 采用 VQFN-HR (14) 封装的 DRV8143S-Q1 SPI 型号",
            current_chapter_titles=("引脚功能",),
            headers=("引脚", "名称"),
        ),
    ]

    result = resolve_document_package_catalog(
        all_tables=[],
        target_tables=targets,
        multi_package_plans={
            target.table_id: MultiPackagePlan(False, "single_package")
            for target in targets
        },
        source_name="DRV8143-Q1_14_28_14.pdf",
    )

    assert [entry.identity_name for entry in result.entries] == [
        "DRV8143H-Q1",
        "DRV8143P-Q1",
        "DRV8143S-Q1",
    ]
    assert [(entry.package_type, entry.pin_count) for entry in result.entries] == [
        ("VQFN", "14"),
        ("HVSSOP", "28"),
        ("VQFN", "14"),
    ]
    assert [
        result.assignment_for(table.table_id, 0).package_key for table in targets
    ] == ["slot:0", "slot:1", "slot:2"]


def test_target_figure_variant_keeps_same_identity_different_packages_separate():
    """同一变体身份下不同封装/引脚数必须保持独立槽位。"""

    targets = [
        PackageTargetTable(
            table_id=1,
            page_idx=1,
            title="表 6-1. 引脚功能",
            group_context="表 6-1. 引脚功能\n图 6-1. 采用 VQFN-HR (16) 封装的 DRV8145H-Q1 HW 型号",
            current_chapter_titles=("引脚配置和功能",),
            headers=("引脚 编号", "引脚 名称"),
        ),
        PackageTargetTable(
            table_id=2,
            page_idx=2,
            title="表 6-2. 引脚功能",
            group_context="表 6-2. 引脚功能\n图 6-2. 采用 HTSSOP (28) 封装的 DRV8145S-Q1 SPI 型号",
            current_chapter_titles=("引脚配置和功能",),
            headers=("引脚 编号", "引脚 名称"),
        ),
        PackageTargetTable(
            table_id=3,
            page_idx=3,
            title="表 6-3. 引脚功能",
            group_context="表 6-3. 引脚功能\n图 6-3. 采用 VQFN-HR(16) 封装的 DRV8145S-Q1 SPI 型号",
            current_chapter_titles=("引脚配置和功能",),
            headers=("引脚 编号", "引脚 名称"),
        ),
    ]

    result = resolve_document_package_catalog(
        all_tables=[],
        target_tables=targets,
        multi_package_plans={
            target.table_id: MultiPackagePlan(False, "single_package")
            for target in targets
        },
        source_name="DRV8145-Q1_16_28.pdf",
    )

    assert [
        (entry.identity_name, entry.package_type, entry.pin_count)
        for entry in result.entries
    ] == [
        ("DRV8145H-Q1", "VQFN", "16"),
        ("DRV8145S-Q1", "HTSSOP", "28"),
        ("DRV8145S-Q1", "VQFN", "16"),
    ]
    assert [
        result.assignment_for(table.table_id, 0).package_key for table in targets
    ] == ["slot:0", "slot:1", "slot:2"]


def test_generic_package_without_pin_count_stays_ambiguous():
    """没有明确 pin_count 时，原有 VQFN 泛化匹配仍保持歧义。"""

    entries = [
        PackageCatalogEntry(
            package_key="slot:0",
            identity_name="DEV-A",
            package_type="VQFN",
            pin_count="20",
        ),
        PackageCatalogEntry(
            package_key="slot:1",
            identity_name="DEV-B",
            package_type="VQFN",
            pin_count="14",
        ),
    ]

    matches = match_entries_in_text(entries, "Figure 1. VQFN package")

    assert [entry.package_key for entry in matches] == ["slot:0", "slot:1"]


def test_group_context_and_same_chapter_continuation_bind_existing_slots():
    """局部上下文可唯一绑定；同章节无重复标题续表可继承该绑定。"""

    summary = catalog_table(
        0,
        "Device Information",
        ["DEVICE", "PACKAGE"],
        [
            ["DEVICE", "PACKAGE"],
            ["DEV-A", "QFN 32"],
            ["DEV-B", "BGA 64"],
        ],
    )
    first = PackageTargetTable(
        table_id=1,
        page_idx=1,
        title="Pin Functions",
        group_context="DEV-B BGA 64 package section",
        current_chapter_titles=("5 Pin Functions",),
        headers=("BALL", "SIGNAL NAME", "TYPE"),
    )
    continued = PackageTargetTable(
        table_id=2,
        page_idx=2,
        title="Pin Functions (continued)",
        group_context="Pin Functions (continued)",
        current_chapter_titles=("5 Pin Functions",),
        headers=("BALL", "SIGNAL NAME", "TYPE"),
    )
    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[first, continued],
        multi_package_plans={
            1: MultiPackagePlan(False, "single_package"),
            2: MultiPackagePlan(False, "single_package"),
        },
        use_semantic_classifier=True,
        classifier=identity_catalog_classifier,
    )

    assert result.assignment_for(1, 0).pkg == "BGA 64"
    assert result.assignment_for(1, 0).reason == "table_group_context"
    assert result.assignment_for(2, 0).pkg == "BGA 64"
    assert result.assignment_for(2, 0).reason == "same_chapter_continuation"


def test_full_pipeline_falls_back_when_all_catalog_slots_are_unresolved():
    """模型接受表格后，全部 package_unresolved 时按单封装继续抽取。"""

    summary = TableCandidate(
        html=(
            "<table><tr><td>DEVICE</td><td>PACKAGE</td></tr>"
            "<tr><td>DEV-A</td><td>QFN 32</td></tr>"
            "<tr><td>DEV-B</td><td>BGA 64</td></tr></table>"
        ),
        page_idx=0,
        title="Device Information",
        group_context="Device Information",
        current_chapter_titles=("Device Information",),
    )
    pin_table = TableCandidate(
        html=(
            "<table><tr><td>PIN NO.</td><td>PIN NAME</td><td>TYPE</td></tr>"
            "<tr><td>1</td><td>VDD</td><td>P</td></tr></table>"
        ),
        page_idx=1,
        title="GPIO Functions",
        group_context="GPIO Functions",
        current_chapter_titles=("GPIO Functions",),
    )
    columns = [
        ColumnDecision(0, "PIN NO.", "pin_no"),
        ColumnDecision(1, "PIN NAME", "pin_name"),
        ColumnDecision(2, "TYPE", "type"),
    ]

    with (
        patch.object(
            pin_extractor,
            "decide_all_tables",
            return_value={1: TableDecision(True, columns=columns)},
        ),
        patch(
            "extract.semantic_classifier.classify_package_catalog_tables",
            side_effect=batch_identity_catalog_classifier,
        ),
    ):
        result = pin_extractor.extract_pin_package_info_from_table_candidates(
            [summary, pin_table],
            source_name="generic-document",
            use_semantic_classifier=True,
            include_debug=True,
        )

    assert result[0]["pkg"] == "QFN 32"
    assert result[0]["group_list"][0]["pin_list"] == [
        {
            "pin_no": "1",
            "pin_name": "VDD",
            "type": "P",
            "source": "generic-document",
            "source_page": 2,
        }
    ]
    debug = pin_extractor.get_last_extraction_debug()
    extracted = next(item for item in debug if item.get("table_id") == 1)
    assert extracted["status"] == "extracted"
    assert extracted["package_assignments"] == [
        {"local_slot": 0, "pkg": "QFN 32", "reason": "single_document_package"}
    ]
    catalog = next(item for item in debug if item.get("table_id") == 0)[
        "package_catalog"
    ]
    fallback = next(
        item
        for item in catalog["diagnostics"]
        if item.get("stage") == "single_package_all_unresolved_fallback"
    )
    assert fallback["status"] == "applied"
