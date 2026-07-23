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
