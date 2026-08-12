"""识别“多个封装分别位于多张引脚表中”的文档结构。

本模块位于两次模型判断之后、文档级 pkg 槽位冻结之前。它不判断一张表
是否应该提取，不选择 pin_no/pin_name/type 列，也不读取数据行生成引脚。

完整分流分成两级：

1. 第一次模型完成引脚表和字段判断后，根据 ``MultiPackagePlan`` 将每张表
   标记为“表内多封装”或“单分支表”。一张表内部存在多套封装字段时继续由
   ``multi_package_extractor.py`` 处理，本模块不会重复分析。
2. 第二次模型返回文档封装目录后，本模块只检查单分支表的表题、完整表头
   和最近章节标题。若多张表分别唯一指向不同封装，则建立“跨表多封装”
   分支；否则保持单封装或 unresolved。

项目规则：

* 表题证据优先于表头，表头优先于最近章节标题。
* 封装 Drawing（例如 RGZ、RKP）优先于器件身份；同一器件的不同 Drawing
  必须保持不同分支。
* 没有 ``Package/封装`` 文字时，已经由第二次模型确认的器件身份或 Drawing
  仍可作为严格匹配证据。
* 声明 pin 数量只保留为原始信息，绝不参与分支分类或表格绑定。
* 相同 Drawing 但器件身份不同的目录项仍是不同槽位；局部表只写 Drawing
  时可能形成歧义，必须留给绑定层记录 unresolved。
* 没有唯一证据的表不创建分支，也不默认绑定第一个 pkg。
* ``parallel_name_columns`` 是同一封装的多个名称模式，不创建 pkg 分支。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


class CatalogEntryLike(Protocol):
    """第二次模型生成的目录项在本模块中需要的字段。"""

    identity_name: str
    identity_aliases: Sequence[str]
    package_type: str
    package_drawing: str
    pin_count: str
    evidence_table_ids: Sequence[int]


class TargetTableLike(Protocol):
    """已经通过第一次模型判断的目标引脚表。"""

    table_id: int
    title: str
    group_context: str
    current_chapter_titles: Sequence[str]
    headers: Sequence[str]


class PackageBindingLike(Protocol):
    package: str


class MultiPackagePlanLike(Protocol):
    is_multi_package: bool
    mode: str
    bindings: Sequence[PackageBindingLike]


@dataclass(frozen=True)
class MultiPkgTabBranch:
    """由一张或多张单分支引脚表共同证明的文档级封装分支。"""

    branch_key: str
    label: str
    evidence_kind: str
    table_ids: tuple[int, ...]
    catalog_entry_indexes: tuple[int, ...] = ()


@dataclass
class MultiPkgTabResolution:
    """跨表结构识别结果；这里只保存证据，不直接创建最终 pkg。"""

    document_mode: str
    table_kinds: dict[int, str]
    table_branch_keys: dict[int, str]
    branches: list[MultiPkgTabBranch]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def branch_for_table(self, table_id: int) -> MultiPkgTabBranch | None:
        """读取单分支表对应的跨表封装分支。"""

        branch_key = self.table_branch_keys.get(table_id)
        if not branch_key:
            return None
        return next(
            (branch for branch in self.branches if branch.branch_key == branch_key),
            None,
        )


@dataclass(frozen=True)
class _BranchEvidence:
    """一张表中找到的唯一封装证据。"""

    branch_key: str
    label: str
    evidence_kind: str
    evidence_source: str
    catalog_entry_indexes: tuple[int, ...] = ()


def resolve_multi_pkg_tab_structure(
    *,
    target_tables: Sequence[TargetTableLike],
    catalog_entries: Sequence[CatalogEntryLike],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
) -> MultiPkgTabResolution:
    """在目录模型返回后完成文档级单/多封装结构分流。

    表内多封装只记录结构类型，不参与跨表分支聚类。其余每张单分支表按
    表题、表头、最近章节标题依次查找唯一证据；同一证据键的续表自然进入
    同一个分支。
    """

    diagnostics: list[dict[str, Any]] = []
    table_kinds: dict[int, str] = {}
    table_evidence: dict[int, _BranchEvidence] = {}
    has_intra_table_multi = False

    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        if _plan_creates_package_slots(plan):
            table_kinds[table.table_id] = "intra_table_multi"
            has_intra_table_multi = True
            diagnostics.append(
                {
                    "stage": "multi_pkg_tab_route",
                    "table_id": table.table_id,
                    "table_kind": "intra_table_multi",
                    "plan_mode": getattr(plan, "mode", ""),
                }
            )
            continue

        # parallel_name_columns 虽然需要多分支读取，但仍只属于一个 pkg。
        table_kinds[table.table_id] = "single_branch"
        evidence, ambiguous = _find_table_branch_evidence(table, catalog_entries)
        if evidence is not None:
            table_evidence[table.table_id] = evidence
            diagnostics.append(
                {
                    "stage": "multi_pkg_tab_route",
                    "table_id": table.table_id,
                    "table_kind": "single_branch",
                    "branch_key": evidence.branch_key,
                    "branch_label": evidence.label,
                    "evidence_kind": evidence.evidence_kind,
                    "evidence_source": evidence.evidence_source,
                    "catalog_entry_indexes": list(evidence.catalog_entry_indexes),
                }
            )
        else:
            diagnostics.append(
                {
                    "stage": "multi_pkg_tab_route",
                    "table_id": table.table_id,
                    "table_kind": "single_branch",
                    "status": "ambiguous" if ambiguous else "no_local_evidence",
                }
            )

    branches = _group_table_evidence(table_evidence)
    table_branch_keys = {
        table_id: evidence.branch_key
        for table_id, evidence in table_evidence.items()
    }

    # 至少两个不同单分支证据，才能证明“多个封装分别位于多张表中”。
    has_cross_table_multi = len(branches) >= 2
    if has_intra_table_multi and has_cross_table_multi:
        document_mode = "mixed_multi_package"
    elif has_intra_table_multi:
        document_mode = "intra_table_multi_package"
    elif has_cross_table_multi:
        document_mode = "cross_table_multi_package"
    elif len(catalog_entries) <= 1:
        document_mode = "single_package"
    else:
        # 目录有多个槽位，但目标表没有形成两个可验证分支，不能猜测分流。
        document_mode = "package_structure_unresolved"

    diagnostics.append(
        {
            "stage": "multi_pkg_tab_document_route",
            "document_mode": document_mode,
            "branch_count": len(branches),
            "branches": [
                {
                    "branch_key": branch.branch_key,
                    "label": branch.label,
                    "evidence_kind": branch.evidence_kind,
                    "table_ids": list(branch.table_ids),
                    "catalog_entry_indexes": list(branch.catalog_entry_indexes),
                }
                for branch in branches
            ],
        }
    )
    return MultiPkgTabResolution(
        document_mode=document_mode,
        table_kinds=table_kinds,
        table_branch_keys=table_branch_keys,
        branches=branches,
        diagnostics=diagnostics,
    )


def catalog_groups_confirmed_by_target_tables(
    resolution: MultiPkgTabResolution,
) -> list[tuple[int, ...]]:
    """返回可由目标引脚表安全证明为同一分支的目录项索引组。"""

    groups: list[tuple[int, ...]] = []
    for branch in resolution.branches:
        indexes = tuple(dict.fromkeys(branch.catalog_entry_indexes))
        if (
            len(indexes) >= 2
            and branch.evidence_kind in {"package_drawing", "explicit_package_label"}
        ):
            groups.append(indexes)
    return groups


def _find_table_branch_evidence(
    table: TargetTableLike,
    entries: Sequence[CatalogEntryLike],
) -> tuple[_BranchEvidence | None, bool]:
    """按证据优先级为一张单分支表寻找唯一分支。"""

    segments: list[tuple[str, str]] = [
        ("table_title", str(table.title or "")),
        ("table_header", " ".join(str(value or "") for value in table.headers)),
    ]
    segments.extend(
        ("nearest_chapter_title", str(title or ""))
        for title in reversed(table.current_chapter_titles)
    )

    saw_ambiguous = False
    seen_segments: set[str] = set()
    for source, text in segments:
        normalized_text = _normalize_text(text)
        if not normalized_text or normalized_text in seen_segments:
            continue
        seen_segments.add(normalized_text)

        # 表题明确写出“XXX Package/XXX 封装”时，XXX 是最强局部分支标签。
        explicit_label = _extract_explicit_package_label(text)
        if explicit_label:
            matches = _match_catalog_label(entries, explicit_label)
            drawing_indexes = tuple(
                index
                for index in matches
                if _normalize_compact(entries[index].package_drawing)
                == _normalize_compact(explicit_label)
            )
            if drawing_indexes and _catalog_matches_are_one_physical_branch(
                entries,
                drawing_indexes,
            ):
                return (
                    _evidence_from_explicit_label(
                        explicit_label,
                        source,
                        entries,
                        drawing_indexes,
                    ),
                    saw_ambiguous,
                )

            # ``DEV100 QFN Package`` 中 QFN 可能被多个器件共用；若表题同时
            # 唯一写明器件身份，应按身份分支，不能把不同 pinout 合并。
            identity_matches = _matches_by_identity(entries, text)
            identity_groups = _group_identity_matches(entries, identity_matches)
            if len(identity_groups) == 1:
                identity_label, identity_indexes = identity_groups[0]
                return (
                    _BranchEvidence(
                        branch_key=f"identity:{_normalize_compact(identity_label)}",
                        label=identity_label,
                        evidence_kind="package_identity",
                        evidence_source=source,
                        catalog_entry_indexes=identity_indexes,
                    ),
                    saw_ambiguous,
                )
            if _catalog_matches_are_one_physical_branch(entries, matches):
                return (
                    _evidence_from_explicit_label(
                        explicit_label,
                        source,
                        entries,
                        matches,
                    ),
                    saw_ambiguous,
                )
            if len(matches) > 1:
                saw_ambiguous = True
                continue
            return (
                _BranchEvidence(
                    branch_key=f"label:{_normalize_compact(explicit_label)}",
                    label=explicit_label,
                    evidence_kind="explicit_package_label",
                    evidence_source=source,
                ),
                saw_ambiguous,
            )

        # Drawing 比器件身份更接近物理封装分支。这里仅建立局部证据，
        # 不使用 pin_count 合并目录项；最终仍需绑定层验证唯一槽位。
        drawing_matches = _matches_by_field(entries, text, "package_drawing")
        drawing_groups = _group_drawing_matches(entries, drawing_matches)
        if len(drawing_groups) == 1:
            label, indexes = drawing_groups[0]
            return (
                _BranchEvidence(
                    branch_key=_drawing_branch_key(entries, indexes, label),
                    label=label,
                    evidence_kind="package_drawing",
                    evidence_source=source,
                    catalog_entry_indexes=indexes,
                ),
                saw_ambiguous,
            )
        if len(drawing_groups) > 1:
            saw_ambiguous = True
            continue

        identity_matches = _matches_by_identity(entries, text)
        identity_groups = _group_identity_matches(entries, identity_matches)
        if len(identity_groups) == 1:
            label, indexes = identity_groups[0]
            return (
                _BranchEvidence(
                    branch_key=f"identity:{_normalize_compact(label)}",
                    label=label,
                    evidence_kind="package_identity",
                    evidence_source=source,
                    catalog_entry_indexes=indexes,
                ),
                saw_ambiguous,
            )
        if len(identity_groups) > 1:
            saw_ambiguous = True

    return None, saw_ambiguous


def _evidence_from_explicit_label(
    label: str,
    source: str,
    entries: Sequence[CatalogEntryLike],
    indexes: tuple[int, ...],
) -> _BranchEvidence:
    """把显式 Package 标签转换成稳定分支键。"""

    drawing_indexes = tuple(
        index
        for index in indexes
        if _normalize_compact(entries[index].package_drawing)
        == _normalize_compact(label)
    )
    if drawing_indexes:
        return _BranchEvidence(
            branch_key=_drawing_branch_key(entries, drawing_indexes, label),
            label=label,
            evidence_kind="package_drawing",
            evidence_source=source,
            catalog_entry_indexes=drawing_indexes,
        )

    identity_indexes = tuple(
        index
        for index in indexes
        if _identity_label_matches(entries[index], label)
    )
    if identity_indexes:
        return _BranchEvidence(
            branch_key=f"identity:{_normalize_compact(label)}",
            label=label,
            evidence_kind="package_identity",
            evidence_source=source,
            catalog_entry_indexes=identity_indexes,
        )

    return _BranchEvidence(
        branch_key=f"label:{_normalize_compact(label)}",
        label=label,
        evidence_kind="explicit_package_label",
        evidence_source=source,
        catalog_entry_indexes=indexes,
    )


def _group_table_evidence(
    table_evidence: Mapping[int, _BranchEvidence],
) -> list[MultiPkgTabBranch]:
    """按文档顺序把同一分支的多张表聚合。"""

    grouped: dict[str, dict[str, Any]] = {}
    for table_id, evidence in table_evidence.items():
        item = grouped.setdefault(
            evidence.branch_key,
            {
                "label": evidence.label,
                "evidence_kind": evidence.evidence_kind,
                "table_ids": [],
                "catalog_entry_indexes": [],
            },
        )
        item["table_ids"].append(table_id)
        item["catalog_entry_indexes"].extend(evidence.catalog_entry_indexes)

    return [
        MultiPkgTabBranch(
            branch_key=branch_key,
            label=item["label"],
            evidence_kind=item["evidence_kind"],
            table_ids=tuple(item["table_ids"]),
            catalog_entry_indexes=tuple(
                dict.fromkeys(item["catalog_entry_indexes"])
            ),
        )
        for branch_key, item in grouped.items()
    ]


def _matches_by_field(
    entries: Sequence[CatalogEntryLike],
    text: str,
    field_name: str,
) -> tuple[int, ...]:
    """按完整标识符边界匹配一个目录字段。"""

    return tuple(
        index
        for index, entry in enumerate(entries)
        if _token_in_text(str(getattr(entry, field_name, "") or ""), text)
    )


def _matches_by_identity(
    entries: Sequence[CatalogEntryLike],
    text: str,
) -> tuple[int, ...]:
    """匹配器件身份及其明确别名。"""

    result = []
    for index, entry in enumerate(entries):
        names = [entry.identity_name, *entry.identity_aliases]
        if any(_token_in_text(str(name or ""), text) for name in names):
            result.append(index)
    return tuple(result)


def _match_catalog_label(
    entries: Sequence[CatalogEntryLike],
    label: str,
) -> tuple[int, ...]:
    """将显式 Package 标签与目录中的身份、Drawing、封装族严格比较。"""

    normalized_label = _normalize_compact(label)
    result = []
    for index, entry in enumerate(entries):
        values = [
            entry.identity_name,
            *entry.identity_aliases,
            entry.package_drawing,
            entry.package_type,
        ]
        if any(_normalize_compact(value) == normalized_label for value in values):
            result.append(index)
    return tuple(result)


def _group_drawing_matches(
    entries: Sequence[CatalogEntryLike],
    indexes: Sequence[int],
) -> list[tuple[str, tuple[int, ...]]]:
    """只按 Drawing 聚合局部证据，pin_count 不参与结构分类。"""

    grouped: dict[str, list[int]] = {}
    labels: dict[str, str] = {}
    for index in indexes:
        entry = entries[index]
        drawing = str(entry.package_drawing or "").strip()
        key = _normalize_compact(drawing)
        if not key:
            continue
        grouped.setdefault(key, []).append(index)
        labels.setdefault(key, drawing)
    return [
        (labels[key], tuple(grouped[key]))
        for key in grouped
    ]


def _group_identity_matches(
    entries: Sequence[CatalogEntryLike],
    indexes: Sequence[int],
) -> list[tuple[str, tuple[int, ...]]]:
    """按被匹配的目录身份分组；不同器件身份不能自动合并。"""

    grouped: dict[str, list[int]] = {}
    labels: dict[str, str] = {}
    for index in indexes:
        identity = str(entries[index].identity_name or "").strip()
        key = _normalize_compact(identity)
        if not key:
            continue
        grouped.setdefault(key, []).append(index)
        labels.setdefault(key, identity)
    return [(labels[key], tuple(grouped[key])) for key in grouped]


def _catalog_matches_are_one_physical_branch(
    entries: Sequence[CatalogEntryLike],
    indexes: Sequence[int],
) -> bool:
    """判断多个匹配是否具有完全一致的器件、封装和 Drawing 三元组。"""

    if len(indexes) <= 1:
        return True
    signatures = {
        (
            _normalize_compact(entries[index].identity_name),
            _normalize_compact(entries[index].package_type),
            _normalize_compact(entries[index].package_drawing),
        )
        for index in indexes
    }
    return len(signatures) == 1 and all(next(iter(signatures)))


def _drawing_branch_key(
    entries: Sequence[CatalogEntryLike],
    indexes: Sequence[int],
    label: str,
) -> str:
    """Drawing 构成局部分支键；目录槽位身份仍由绑定层严格判断。"""

    return f"drawing:{_normalize_compact(label)}"


def _identity_label_matches(entry: CatalogEntryLike, label: str) -> bool:
    normalized_label = _normalize_compact(label)
    return any(
        _normalize_compact(name) == normalized_label
        for name in [entry.identity_name, *entry.identity_aliases]
    )


def _extract_explicit_package_label(value: str) -> str:
    """只从明确的 Package/封装短语读取局部标签。"""

    text = str(value or "")
    patterns = (
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_-]{0,20})\s+(?:package|pkg)\b",
        r"\b(?:package|pkg)\s*[-:：]?\s*([A-Za-z][A-Za-z0-9_-]{0,20})(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_-]{0,20})\s*封装",
    )
    rejected = {
        "a",
        "the",
        "device",
        "physical",
        "information",
        "pin",
        "ball",
    }
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            label = match.group(1).strip(" -_:：")
            if _normalize_text(label) not in rejected:
                return label
    return ""


def _token_in_text(token: str, text: str) -> bool:
    """完整边界匹配，避免 RKP 命中另一个更长标识符的内部。"""

    token = str(token or "").strip()
    if not token:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def _plan_creates_package_slots(plan: MultiPackagePlanLike | None) -> bool:
    """表内真正的多封装分支才创建 pkg；名称模式分支不创建。"""

    return bool(
        plan is not None
        and plan.is_multi_package
        and plan.mode != "parallel_name_columns"
        and len(plan.bindings) >= 2
    )


def _clean_pin_count(value: Any) -> str:
    match = re.search(r"\d+", str(value or ""))
    return match.group(0) if match else ""


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _normalize_compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _normalize_text(value))
