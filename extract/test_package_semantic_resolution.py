"""封装语义发现和表级归属的通用结构测试。

测试使用抽象文档块和伪模型返回，不依赖任何具体 PDF，也不访问外部 API。
"""

from __future__ import annotations

from dataclasses import dataclass

from extract.package_resolver import (
    PackageDocumentBlock,
    PackageTableSource,
    PackageTargetTable,
    resolve_document_packages,
)
from extract.pin_package_extractor import (
    ExtractedGroup,
    PackageIdentity,
    build_public_result,
    get_package_bucket,
)


@dataclass(frozen=True)
class _Column:
    index: int
    raw_header: str
    field_name: str


@dataclass(frozen=True)
class _Plan:
    is_multi_package: bool = False
    mode: str = ""
    bindings: tuple = ()


@dataclass(frozen=True)
class _Binding:
    package: str
    pin_no_column: int
    pin_name_column: int | None = None
    row_indexes: frozenset[int] | None = None


PIN_COLUMNS = (
    _Column(0, "BALL NUMBER", "pin_no"),
    _Column(1, "SIGNAL NAME", "pin_name"),
)


def _source(table_id: int, title: str, rows: tuple[tuple[str, ...], ...]) -> PackageTableSource:
    return PackageTableSource(
        table_id=table_id,
        title=title,
        group_context=title,
        previous_chapter_titles=(),
        current_chapter_titles=("5 Pin Functions",),
        rows=rows,
    )


def _target(
    table_id: int,
    title: str,
    headers: tuple[str, ...] = ("BALL NUMBER", "SIGNAL NAME"),
) -> PackageTargetTable:
    return PackageTargetTable(
        table_id=table_id,
        title=title,
        current_chapter_titles=("5 Pin Functions",),
        headers=headers,
        data_rows=(("A1", "SIG_A"), ("A2", "SIG_B")),
        columns=PIN_COLUMNS,
    )


def test_semantic_entity_requires_exact_evidence_text() -> None:
    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "Devices are offered using the SF2507 and SF2507E package options.",
        ),
    )

    def classify(_blocks):
        return {
            "entities": [
                {
                    "name": "SF2507",
                    "role": "package_identity",
                    "aliases": [],
                    "evidence_block_ids": ["block-0"],
                },
                {
                    "name": "MADE_UP",
                    "role": "package_identity",
                    "aliases": [],
                    "evidence_block_ids": ["block-0"],
                },
            ]
        }

    result = resolve_document_packages(
        all_tables=[_source(0, "Table 5-1. Pin Data", (("A1", "SIG_A"),))],
        target_tables=[],
        multi_package_plans={},
        document_blocks=blocks,
        entity_classifier=classify,
    )

    assert result.registry.unique_key_for_label("SF2507")
    assert result.registry.unique_key_for_label("MADE_UP") == ""
    assert any(item["status"] == "rejected" for item in result.diagnostics)


def test_specific_header_beats_generic_family_in_title() -> None:
    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "The SF2507 package uses an LQFP family body.",
        ),
    )

    def classify(_blocks):
        return {
            "entities": [
                {
                    "name": "SF2507",
                    "role": "package_identity",
                    "family": "LQFP",
                    "aliases": [],
                    "evidence_block_ids": ["block-0"],
                }
            ]
        }

    source = _source(0, "Table 5-1. LQFP Package Pins", (("A1", "SIG_A"),))
    target = _target(
        0,
        source.title,
        ("SF2507 BALL NUMBER", "SIGNAL NAME"),
    )
    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[target],
        multi_package_plans={},
        document_blocks=blocks,
        entity_classifier=classify,
    )

    assert result.package_label(0) == "SF2507"
    assert result.assignments[0].mode == "table_header"


def test_family_plus_pin_count_is_not_a_concrete_package() -> None:
    source = _source(
        0,
        "Package Information",
        (
            ("Package Name", "Package Drawing", "Pins"),
            ("nFBGA (48 Pin)", "ZXH", "48"),
        ),
    )
    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[],
        multi_package_plans={},
    )

    zxh_key = result.registry.unique_key_for_label("ZXH")
    assert result.registry.candidates[zxh_key].role == "package_identity"
    assert result.registry.display_for_key(zxh_key) == "ZXH"
    assert result.registry.specific_keys_in_text("nFBGA (48 Pin)") == set()


def test_semantic_family_aliases_are_not_appended_to_pkg() -> None:
    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "Package drawing ZQE is supplied in the MicroStar Jr. BGA family.",
        ),
    )

    def classify(_blocks):
        return {
            "entities": [
                {
                    "name": "ZQE",
                    "role": "package_identity",
                    "aliases": ["MicroStar Jr. BGA"],
                    "family": "BGA",
                    "evidence_block_ids": ["block-0"],
                }
            ]
        }

    result = resolve_document_packages(
        all_tables=[],
        target_tables=[],
        multi_package_plans={},
        document_blocks=blocks,
        entity_classifier=classify,
    )

    assert result.registry.display_for_label("ZQE") == "ZQE"


def test_conflicting_specific_title_and_header_do_not_guess() -> None:
    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "AAA and BBB are different package drawings.",
        ),
    )

    def classify(_blocks):
        return {
            "entities": [
                {
                    "name": "AAA",
                    "role": "package_identity",
                    "aliases": [],
                    "evidence_block_ids": ["block-0"],
                },
                {
                    "name": "BBB",
                    "role": "package_identity",
                    "aliases": [],
                    "evidence_block_ids": ["block-0"],
                },
            ]
        }

    source = _source(0, "Table 5-1. AAA Package Pins", (("A1", "SIG_A"),))
    target = _target(0, source.title, ("BBB BALL NUMBER", "SIGNAL NAME"))
    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[target],
        multi_package_plans={},
        document_blocks=blocks,
        entity_classifier=classify,
    )

    assert result.package_label(0) == ""
    assert result.assignments[0].mode == "conflict"
    assert "表题与表头" in result.assignments[0].reason


def test_semantic_assignment_can_only_select_existing_package_id() -> None:
    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "Package drawing ZCE applies to the following connectivity table.",
        ),
        PackageDocumentBlock("block-1", 1, "table", "A1 SIG_A", table_id=0),
    )

    def discover(_blocks):
        return {
            "entities": [
                {
                    "name": "ZCE",
                    "role": "package_identity",
                    "aliases": [],
                    "evidence_block_ids": ["block-0"],
                }
            ]
        }

    def assign(_target, _blocks, candidates):
        return {
            "table_id": 0,
            "package_id": candidates[0].key,
            "evidence_block_ids": ["block-0"],
        }

    source = _source(0, "Table 5-2. Connectivity Requirements", (("A1", "SIG_A"),))
    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[_target(0, source.title)],
        multi_package_plans={0: _Plan()},
        document_blocks=blocks,
        entity_classifier=discover,
        assignment_classifier=assign,
    )

    assert result.package_label(0) == "ZCE"
    assert result.assignments[0].mode == "semantic_context"


def test_aliases_are_internal_and_pkg_is_one_name() -> None:
    """同一封装的多个别名不能再通过竖线拼成一个 pkg。"""

    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "ZCE and ZCE-64 are explicit names for the same package option.",
        ),
    )

    def discover(_blocks):
        return {
            "entities": [
                {
                    "name": "ZCE",
                    "role": "package_identity",
                    "aliases": ["ZCE-64"],
                    "evidence_block_ids": ["block-0"],
                }
            ]
        }

    result = resolve_document_packages(
        all_tables=[],
        target_tables=[],
        multi_package_plans={},
        document_blocks=blocks,
        entity_classifier=discover,
    )

    package_key = result.registry.unique_key_for_label("ZCE")
    assert package_key
    assert result.registry.candidates[package_key].aliases == ["ZCE", "ZCE-64"]
    assert result.registry.display_for_key(package_key) == "ZCE"
    assert "|" not in result.registry.display_for_key(package_key)


def test_table_title_selects_the_alias_used_by_current_table() -> None:
    """同一实体有多个名称时，当前表题中的名称必须优先输出。"""

    blocks = (
        PackageDocumentBlock(
            "block-0",
            0,
            "paragraph",
            "ZCE and ZCE-64 are explicit names for the same package option.",
        ),
        PackageDocumentBlock(
            "block-1",
            1,
            "table",
            "Table 5-1. ZCE-64 Package Pin Data",
            table_id=0,
        ),
    )

    def discover(_blocks):
        return {
            "entities": [
                {
                    "name": "ZCE",
                    "role": "package_identity",
                    "aliases": ["ZCE-64"],
                    "evidence_block_ids": ["block-0"],
                }
            ]
        }

    source = _source(
        0,
        "Table 5-1. ZCE-64 Package Pin Data",
        (("A1", "SIG_A"),),
    )
    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[_target(0, source.title)],
        multi_package_plans={},
        document_blocks=blocks,
        entity_classifier=discover,
    )

    assert result.assignments[0].mode == "table_title"
    assert result.assignments[0].selected_label == "ZCE-64"
    assert result.package_label(0) == "ZCE-64"
    assert "|" not in result.package_label(0)


def test_multi_package_labels_are_not_combined() -> None:
    """不同封装标签必须保持为多个独立名称，不能合成一个字符串。"""

    source = _source(
        0,
        "Table 3-4. Pin Descriptions",
        (
            ("Pin Name", "SSOP", "QFN", "LQFP"),
            ("DM0", "25", "1", "3"),
        ),
    )
    plan = _Plan(
        is_multi_package=True,
        mode="package_specific_columns",
        bindings=(
            _Binding("SSOP", 1),
            _Binding("QFN", 2),
            _Binding("LQFP", 3),
        ),
    )
    result = resolve_document_packages(
        all_tables=[source],
        target_tables=[_target(0, source.title)],
        multi_package_plans={0: plan},
    )

    labels = [
        result.registry.display_for_label(label)
        for label in ("SSOP 28", "QFN 28", "LQFP 48")
    ]
    assert labels == ["SSOP 28", "QFN 28", "LQFP 48"]
    assert all("|" not in label for label in labels)


def test_final_bucket_keeps_one_highest_priority_alias() -> None:
    """最终封装桶可记录别名，但公开 JSON 只输出证据更强的一个名称。"""

    packages = {}
    bucket = get_package_bucket(
        packages,
        PackageIdentity("ZCE", "drawing=zce", priority=70),
    )
    bucket["_groups"]["pins"] = ExtractedGroup(
        "pins",
        [{"pin_no": "1", "pin_name": "VDD", "type": "P"}],
    )
    get_package_bucket(
        packages,
        PackageIdentity("ZCE-64", "drawing=zce", priority=100),
    )

    result = build_public_result(packages, include_debug=False)

    assert result[0]["pkg"] == "ZCE-64"
    assert "|" not in result[0]["pkg"]
    assert bucket["_aliases"] == ["ZCE", "ZCE-64"]


def test_different_package_keys_create_different_outer_objects() -> None:
    """不同封装即使来自同一张多封装表，也必须输出为不同外层对象。"""

    packages = {}
    for package_name in ("SSOP", "QFN", "LQFP"):
        bucket = get_package_bucket(
            packages,
            PackageIdentity(
                package_name,
                f"label={package_name.lower()}",
                priority=110,
            ),
        )
        bucket["_groups"]["pins"] = ExtractedGroup(
            "pins",
            [{"pin_no": "1", "pin_name": "VDD", "type": "P"}],
        )

    result = build_public_result(packages, include_debug=False)

    assert [item["pkg"] for item in result] == ["SSOP", "QFN", "LQFP"]
