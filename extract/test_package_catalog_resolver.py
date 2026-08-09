"""文档级封装目录和表格绑定的通用测试。"""

from unittest.mock import patch

import extract.pin_package_extractor as pin_extractor
from extract.package_catalog_resolver import (
    PackageCatalogTable,
    PackageTargetTable,
    clean_package_name,
    find_package_catalog_candidates,
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
            "extract.semantic_classifier.classify_package_catalog_table",
            side_effect=package_classifier,
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


def test_multi_package_without_unique_evidence_stays_unresolved():
    """多封装文档的无归属表不能再默认塞入第一个 pkg。"""

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

    assert result.assignment_for(1, 0) is None
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.get("stage") == "package_binding" and item.get("table_id") == 1
    )
    assert diagnostic["status"] == "unresolved"
    assert diagnostic["reason"] == "package_unresolved"
    assert diagnostic["document_packages"] == ["QFN 32", "BGA 64"]


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


def test_full_pipeline_skips_unresolved_table_before_row_extraction():
    """模型接受表格后，pkg 未唯一绑定时仍必须整表跳过。"""

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
            "extract.semantic_classifier.classify_package_catalog_table",
            side_effect=identity_catalog_classifier,
        ),
    ):
        result = pin_extractor.extract_pin_package_info_from_table_candidates(
            [summary, pin_table],
            source_name="generic-document",
            use_semantic_classifier=True,
            include_debug=True,
        )

    assert all(not package["group_list"] for package in result)
    debug = pin_extractor.get_last_extraction_debug()
    skipped = next(item for item in debug if item.get("table_id") == 1)
    assert skipped["status"] == "skipped"
    assert skipped["skip_reason"] == "package_unresolved"
    assert skipped["package_binding"]["unresolved_local_slots"] == [0]
