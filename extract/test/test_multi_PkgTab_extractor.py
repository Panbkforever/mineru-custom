"""跨表多封装分流的通用测试。"""

from dataclasses import dataclass, field

from extract.multi_PkgTab_extractor import (
    catalog_groups_confirmed_by_target_tables,
    resolve_multi_pkg_tab_structure,
)
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.package_catalog_resolver import (
    PackageCatalogTable,
    PackageTargetTable,
    resolve_document_package_catalog,
)


@dataclass
class Entry:
    identity_name: str = ""
    identity_aliases: list[str] = field(default_factory=list)
    package_type: str = ""
    package_drawing: str = ""
    pin_count: str = ""
    evidence_table_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Table:
    table_id: int
    title: str
    headers: tuple[str, ...] = ("PIN NO", "PIN NAME", "TYPE")
    group_context: str = ""
    current_chapter_titles: tuple[str, ...] = ()


def single_plan() -> MultiPackagePlan:
    return MultiPackagePlan(False, "single_package")


def catalog_classifier(table, source_name, target_tables):
    """测试用第二次模型：只返回目录表的列结构。"""

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


def catalog_table(rows):
    """生成通用封装总述表，不依赖具体 PDF 文件名。"""

    return PackageCatalogTable(
        table_id=0,
        page_idx=0,
        title="Package Information",
        group_context="Package Information",
        current_chapter_titles=("Package Information",),
        headers=("DEVICE", "PACKAGE", "DRAWING", "PINS"),
        rows=tuple(tuple(row) for row in rows),
    )


def package_target(table_id, title, chapter):
    """生成已经通过第一次模型判断的单分支引脚表。"""

    return PackageTargetTable(
        table_id=table_id,
        page_idx=table_id,
        title=title,
        group_context=title,
        current_chapter_titles=(chapter,),
        headers=("PIN NO", "PIN NAME", "TYPE"),
    )


def test_separate_package_tables_form_cross_table_branches():
    entries = [
        Entry("DEV-A", package_type="VQFN", package_drawing="RGZ", pin_count="48"),
        Entry("DEV-B", package_type="VQFN", package_drawing="RKP", pin_count="40"),
    ]
    tables = [
        Table(10, "Table 4-1. Signal Descriptions - RGZ Package"),
        Table(11, "Table 4-2. Unused Pins - RGZ Package"),
        Table(12, "Table 5-1. Signal Descriptions - RKP Package"),
    ]

    result = resolve_multi_pkg_tab_structure(
        target_tables=tables,
        catalog_entries=entries,
        multi_package_plans={table.table_id: single_plan() for table in tables},
    )

    assert result.document_mode == "cross_table_multi_package"
    assert len(result.branches) == 2
    assert result.table_branch_keys[10] == result.table_branch_keys[11]
    assert result.table_branch_keys[10] != result.table_branch_keys[12]


def test_same_drawing_catalog_rows_merge_only_with_target_evidence():
    entries = [
        Entry("DEV-A1", package_type="VQFN", package_drawing="RGZ", pin_count="48"),
        Entry("DEV-A2", package_type="VQFN", package_drawing="RGZ", pin_count="48"),
        Entry("DEV-B", package_type="VQFN", package_drawing="RKP", pin_count="40"),
    ]
    tables = [
        Table(1, "Pin Functions - RGZ Package"),
        Table(2, "Pin Functions - RKP Package"),
    ]

    result = resolve_multi_pkg_tab_structure(
        target_tables=tables,
        catalog_entries=entries,
        multi_package_plans={1: single_plan(), 2: single_plan()},
    )

    assert catalog_groups_confirmed_by_target_tables(result) == [(0, 1)]


def test_identity_separates_models_that_share_one_package_family():
    entries = [
        Entry("DEV100", package_type="QFN", package_drawing="RGE", pin_count="36"),
        Entry("DEV200", package_type="QFN", package_drawing="RGE", pin_count="36"),
    ]
    tables = [
        Table(1, "Table 3-1. DEV100 QFN Package Pin-out"),
        Table(2, "Table 4-1. DEV200 QFN Package Pin-out"),
    ]

    result = resolve_multi_pkg_tab_structure(
        target_tables=tables,
        catalog_entries=entries,
        multi_package_plans={1: single_plan(), 2: single_plan()},
    )

    assert result.document_mode == "cross_table_multi_package"
    assert {branch.label for branch in result.branches} == {"DEV100", "DEV200"}
    assert catalog_groups_confirmed_by_target_tables(result) == []


def test_intra_table_package_columns_stay_in_existing_branch():
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("PKG-A", 1, 0, 3),
            PackageBinding("PKG-B", 2, 0, 3),
        ),
    )

    result = resolve_multi_pkg_tab_structure(
        target_tables=[Table(1, "Pin Functions")],
        catalog_entries=[],
        multi_package_plans={1: plan},
    )

    assert result.document_mode == "intra_table_multi_package"
    assert result.table_kinds[1] == "intra_table_multi"
    assert result.branches == []


def test_parallel_name_columns_do_not_create_package_slots():
    plan = MultiPackagePlan(
        True,
        "parallel_name_columns",
        bindings=(
            PackageBinding("MODE-A", 1, 0, 3),
            PackageBinding("MODE-B", 2, 0, 3),
        ),
    )

    result = resolve_multi_pkg_tab_structure(
        target_tables=[Table(1, "Pin Functions")],
        catalog_entries=[Entry(package_type="QFN")],
        multi_package_plans={1: plan},
    )

    assert result.document_mode == "single_package"
    assert result.table_kinds[1] == "single_branch"


def test_multiple_catalog_slots_without_target_evidence_stay_unresolved():
    result = resolve_multi_pkg_tab_structure(
        target_tables=[Table(1, "Pin Functions")],
        catalog_entries=[
            Entry(package_type="QFN", package_drawing="AAA"),
            Entry(package_type="BGA", package_drawing="BBB"),
        ],
        multi_package_plans={1: single_plan()},
    )

    assert result.document_mode == "package_structure_unresolved"
    assert result.table_branch_keys == {}


def test_catalog_pipeline_binds_separate_tables_to_stable_package_slots():
    """跨表分流必须真正进入文档级 assignment，不能只停留在诊断结果。"""

    summary = catalog_table(
        [
            ("DEVICE", "PACKAGE", "DRAWING", "PINS"),
            ("DEV-A", "VQFN", "RGZ", "48"),
            ("DEV-B", "VQFN", "RKP", "40"),
        ]
    )
    targets = [
        package_target(1, "Table 4-1. RGZ Package Pin Functions", "4 RGZ"),
        package_target(2, "Table 4-2. RGZ Package Unused Pins", "4 RGZ"),
        package_target(3, "Table 5-1. RKP Package Pin Functions", "5 RKP"),
    ]
    plans = {table.table_id: single_plan() for table in targets}

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=targets,
        multi_package_plans=plans,
        use_semantic_classifier=True,
        classifier=catalog_classifier,
    )

    assert len(result.entries) == 2
    rgz_main = result.assignment_for(1, 0)
    rgz_unused = result.assignment_for(2, 0)
    rkp_main = result.assignment_for(3, 0)
    assert rgz_main is not None
    assert rgz_unused is not None
    assert rkp_main is not None
    assert rgz_main.package_key == rgz_unused.package_key
    assert rgz_main.package_key != rkp_main.package_key
    assert rgz_main.reason == "cross_table_package_branch"
    assert rkp_main.reason == "cross_table_package_branch"


def test_catalog_pipeline_keeps_intra_table_multi_package_routing():
    """一表多封装继续按列分支绑定，不得被跨表模块改成单分支。"""

    summary = catalog_table(
        [
            ("DEVICE", "PACKAGE", "DRAWING", "PINS"),
            ("DEV-A", "VQFN", "RGZ", "48"),
            ("DEV-B", "WQFN", "RKP", "40"),
        ]
    )
    target = package_target(1, "Table 4-1. Package Pin Functions", "4 Pins")
    plan = MultiPackagePlan(
        True,
        "package_columns",
        bindings=(
            PackageBinding("RGZ", 1, 0, 3),
            PackageBinding("RKP", 2, 0, 3),
        ),
    )

    result = resolve_document_package_catalog(
        all_tables=[summary],
        target_tables=[target],
        multi_package_plans={1: plan},
        use_semantic_classifier=True,
        classifier=catalog_classifier,
    )

    first = result.assignment_for(1, 0)
    second = result.assignment_for(1, 1)
    assert first is not None
    assert second is not None
    assert first.package_key != second.package_key
    assert first.reason == "multi_package_global_unique_binding"
    assert second.reason == "multi_package_global_unique_binding"
