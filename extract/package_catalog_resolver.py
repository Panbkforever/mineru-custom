"""建立文档级物理封装槽位，并把已确认的引脚表绑定到槽位。

本模块位于“表格/字段判断”之后、“逐行生成引脚记录”之前。它只处理
最终 JSON 最外层的 ``pkg``，不判断 pin_no、pin_name、type，也不修改
任何表格行内容。

固定处理流程：

1. 从全文表格中定位可能的封装总述表或包装信息表。当前表自己的表题命中
   器件信息、封装信息、订购信息及对应英文时属于最高优先级；否则只从
   目录前、目录结束后三页或文档末十页等限定页面区域召回。章节标题不能
   代替当前表题触发最高优先级。
2. 模型只判断表格角色和字段位置；代码按这些位置从原表保留 device、
   package、drawing、declared_pin_count 原始事实，模型不能合并事实。
3. 原始事实与输出槽位分开保存。只有 device、package、drawing 三项都完整
   且全部一致的事实才允许合并；pin_count 只记录，绝不参与合并或绑定。
4. 已确认引脚表中的明确封装分支可以反向补全目录缺失槽位，并记录
   ``catalog_conflict``；普通数据值不能用于猜测 pkg。
5. 封装槽位只能来自第二次模型抄录的原始事实，或已确认引脚表中明确的
   封装分支表头。禁止根据声明 pin 数、候选表数量或预期结果克隆槽位。
6. 目标引脚表只通过表题、章节标题、表头和多封装列标签绑定已有槽位；
   description 和数据行不能参与绑定。
7. 槽位冻结后，任何未匹配表都不能创建新 pkg。单封装文档可以绑定唯一
   槽位；多封装文档中无法唯一归属的表必须标记为 unresolved，禁止默认
   塞入第一个槽位。真实名称缺失时仅对已经确认的槽位使用 a、b、c……。
8. 多封装表的全部本地分支必须一次性执行一对一绑定；禁止每个分支独立
   兜底后落入同一个 package_key。标签脚注清理后的 device、package 和
   drawing 可以提供精确匹配证据；pin_count 只进入诊断，永不参与消歧、
   槽位合并或表格绑定，也不改变任何引脚行内容。
9. ``XXX Mode Pin Name`` 形成的运行模式分支只控制名称列读取，不创建 pkg
   槽位；这些分支必须共同绑定当前表所属的同一个物理封装。

特别重要的边界：

* 这里不会再次调用引脚表字段判断，也不会生成引脚记录。
* ``multi_package_extractor.py`` 仍只负责单张表内部的多封装结构。
* description 和普通正文不能参与封装绑定。
* 一个 pkg 只能是一个字符串，禁止使用 ``|`` 拼接多个候选名称。
* pkg 名称最长 15 个字符；超过长度的标题、描述或多个名称拼接结果直接拒绝。
* 器件型号、订购型号和 Drawing 不能写入公开 pkg；Drawing 只用于消歧。
* 不能为每张未匹配表生成 ``unresolved:table_id``，否则表数会被误当成封装数。
* 两个槽位即使公开封装名相同也保持独立；只有建立槽位时的同一行/同一结构
  证据才能决定它们是不是同一个物理映射空间。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from extract.multi_PkgTab_extractor import (
    MultiPkgTabResolution,
    resolve_multi_pkg_tab_structure,
)


class PackageBindingLike(Protocol):
    """多封装绑定对象在本模块中需要的最小字段集合。"""

    package: str


class MultiPackagePlanLike(Protocol):
    """多封装计划在文档级封装绑定中需要的最小接口。"""

    is_multi_package: bool
    mode: str
    bindings: Sequence[PackageBindingLike]


@dataclass(frozen=True)
class PackageCatalogTable:
    """全文扫描阶段的一张原始表格。"""

    table_id: int
    page_idx: int | None
    title: str
    group_context: str
    current_chapter_titles: tuple[str, ...]
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class PackageTargetTable:
    """已经通过引脚表/字段判断、等待绑定 pkg 的目标表。"""

    table_id: int
    page_idx: int | None
    title: str
    group_context: str
    current_chapter_titles: tuple[str, ...]
    headers: tuple[str, ...]


@dataclass(frozen=True)
class RawPackageFact:
    """第二次模型确认字段后，从原始表格读取的一条封装事实。

    该对象只保存证据，不等同于输出槽位。``declared_pin_count`` 便于调试和
    校验，但不得进入槽位合并键或目标表绑定分数。
    """

    device: str = ""
    package: str = ""
    drawing_code: str = ""
    declared_pin_count: str = ""
    source_table_id: int = -1
    source_row_index: int = -1
    table_role: str = ""


@dataclass
class PackageCatalogEntry:
    """一个已经冻结的物理封装槽位。

    ``identity_name`` 是器件型号，只参与表格关联；``package_type`` 才是
    最终 JSON 中允许公开的物理封装名称。
    """

    package_key: str
    identity_name: str = ""
    identity_aliases: list[str] = field(default_factory=list)
    package_type: str = ""
    package_drawing: str = ""
    pin_count: str = ""
    evidence_table_ids: list[int] = field(default_factory=list)
    raw_facts: list[RawPackageFact] = field(default_factory=list)
    origin: str = "package_catalog"


@dataclass(frozen=True)
class PackageColumnRole:
    """模型确认的一列语义；这里只保存列索引和角色，不保存单元格值。"""

    column_index: int
    role: str
    header: str = ""


@dataclass(frozen=True)
class PackageCatalogRawValue:
    """第二次模型对一条原始封装记录的逐字段抄录结果。"""

    device: str = ""
    package: str = ""
    drawing_code: str = ""
    declared_pin_count: str = ""
    source_row_index: int = -1


@dataclass(frozen=True)
class PackageCatalogDecision:
    """模型对一张候选表的结构判断。"""

    is_package_summary: bool
    table_role: str
    header_row_index: int
    columns: tuple[PackageColumnRole, ...] = ()
    raw_values: tuple[PackageCatalogRawValue, ...] = ()


@dataclass(frozen=True)
class PackageAssignment:
    """目标表的一个本地封装槽位对应的文档级封装。"""

    package_key: str
    pkg: str
    reason: str


@dataclass
class PackageCatalogResolution:
    """整篇文档的封装目录、绑定结果和调试信息。"""

    entries: list[PackageCatalogEntry]
    assignments: dict[tuple[int, int], PackageAssignment]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    raw_facts: list[RawPackageFact] = field(default_factory=list)
    document_route: str = "package_structure_unresolved"

    def assignment_for(
        self,
        table_id: int,
        local_slot: int,
    ) -> PackageAssignment | None:
        """读取表内槽位绑定；缺失就是 unresolved，禁止隐式回退。"""

        return self.assignments.get((table_id, local_slot))

    def declared_assignments(self) -> list[PackageAssignment]:
        """按固定槽位顺序返回输出桶，未命名槽位使用 a/b/c。"""

        return [
            assignment_from_entry(
                entry,
                self.entries,
                reason="declared_package_slot",
            )
            for entry in self.entries
        ]


PackageCatalogClassifier = Callable[..., Mapping[str, Any]]


def resolve_document_package_catalog(
    *,
    all_tables: Sequence[PackageCatalogTable],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    source_name: str = "",
    use_semantic_classifier: bool = False,
    classifier: PackageCatalogClassifier | None = None,
    document_page_count: int | None = None,
    toc_page_range: tuple[int, int] | None = None,
) -> PackageCatalogResolution:
    """建立封装目录并为每张目标表生成唯一绑定结果。

    ``all_tables`` 包含全文表格，因此订购表、Device Information 等即使不属于
    引脚表，也仍可以成为封装总述候选。``target_tables`` 只包含已经确认要
    提取的物理引脚表，两者职责不能混用。
    """

    diagnostics: list[dict[str, Any]] = []
    entries: list[PackageCatalogEntry] = []
    raw_facts: list[RawPackageFact] = []

    # 已经由第一次模型确认的引脚表不能再次作为封装总述候选送入第二次
    # 模型。两个模型阶段职责独立，避免重复请求和相互污染。
    target_table_ids = {table.table_id for table in target_tables}
    catalog_candidates = find_package_catalog_candidates(
        all_tables,
        document_page_count=document_page_count,
        toc_page_range=toc_page_range,
        excluded_table_ids=target_table_ids,
    )
    if use_semantic_classifier or classifier is not None:
        entries, semantic_diagnostics = classify_package_catalog_candidates(
            catalog_candidates,
            source_name=source_name,
            target_tables=target_tables,
            classifier=classifier,
        )
        diagnostics.extend(semantic_diagnostics)
        raw_facts = [fact for entry in entries for fact in entry.raw_facts]

    # 引脚表表头中的明确封装分支是强证据。目录缺少分支时在冻结 slot 之前
    # 补槽位，并留下 catalog_conflict；普通数据行永远不能触发该补全。
    entries = supplement_slots_from_explicit_table_branches(
        entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
        diagnostics=diagnostics,
    )
    # 表头反向补全产生的强证据也属于本次解析的原始事实，调试信息必须
    # 覆盖完整槽位来源，不能只记录第二次模型返回的事实。
    raw_facts = [fact for entry in entries for fact in entry.raw_facts]

    # 第二次模型只给出封装目录，不负责判断这些封装是位于同一张表的不同
    # 字段，还是分别位于多张表。这里先完成文档结构分流，再冻结 slot。
    multi_pkg_tab_resolution = resolve_multi_pkg_tab_structure(
        target_tables=target_tables,
        catalog_entries=entries,
        multi_package_plans=multi_package_plans,
    )
    diagnostics.extend(multi_pkg_tab_resolution.diagnostics)
    # 旧版 reconcile/merge_plan 会按 pin_count、封装族或分支数量修剪/克隆
    # 目录项。新主流程不再调用这些函数，槽位只能来自原始事实或明确表头。
    if not entries and target_tables:
        entries = [
            PackageCatalogEntry(
                package_key="",
                evidence_table_ids=[target_tables[0].table_id],
            )
        ]

    # 到这里封装数量已经确定。后续绑定只能选择这些 slot，不能增删。
    freeze_package_slots(entries)

    assignments = bind_target_tables(
        entries=entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
        multi_pkg_tab_resolution=multi_pkg_tab_resolution,
        diagnostics=diagnostics,
    )
    return PackageCatalogResolution(
        entries=entries,
        assignments=assignments,
        diagnostics=diagnostics,
        raw_facts=raw_facts,
        document_route=multi_pkg_tab_resolution.document_mode,
    )


def find_package_catalog_candidates(
    tables: Sequence[PackageCatalogTable],
    *,
    document_page_count: int | None = None,
    toc_page_range: tuple[int, int] | None = None,
    excluded_table_ids: set[int] | frozenset[int] = frozenset(),
) -> list[PackageCatalogTable]:
    """定位可能记录封装总数和名称的总述表。

    该阶段只负责召回，不能直接认定 pkg。候选集合严格由以下来源并集组成：

    * 当前表自己的表题命中高优先级名称；
    * 有目录时：目录起始页之前，以及目录结束页之后的三页；
    * 无目录时：文档前十页；
    * 无论是否有目录：文档最后十页。

    已由第一次模型确认的引脚表必须排除。候选按“高优先级表题优先，其余
    保持文档顺序”返回，同一张表只出现一次。
    """

    if not tables:
        return []

    known_pages = [
        table.page_idx
        for table in tables
        if isinstance(table.page_idx, int) and table.page_idx >= 0
    ]
    if document_page_count is None and known_pages:
        document_page_count = max(known_pages) + 1

    priority: list[PackageCatalogTable] = []
    regional: list[PackageCatalogTable] = []
    for table in tables:
        if table.table_id in excluded_table_ids:
            continue
        if not _has_catalog_table_shape(table):
            continue

        # 最高优先级只看当前表自己的 title。group_context 和章节标题仍会
        # 传给模型作为语义上下文，但不能把整章普通表都提升为候选。
        if _has_priority_catalog_title(table.title):
            priority.append(table)
            continue

        if _is_catalog_candidate_page(
            table.page_idx,
            document_page_count=document_page_count,
            toc_page_range=toc_page_range,
        ):
            regional.append(table)

    # 最终 Markdown 缺少 middle_json 时没有可靠页码。此时不能恢复旧的
    # “前后 15% 表格”猜测，只保留有自身表题证据的候选，避免请求量失控。
    return [*priority, *regional]


def _has_catalog_table_shape(table: PackageCatalogTable) -> bool:
    """封装总述模型只接收至少一行、至少两列的二维表。"""

    return (
        len(table.rows) >= 1
        and max((len(row) for row in table.rows), default=0) >= 2
    )


def _has_priority_catalog_title(title: str) -> bool:
    """只检查当前表题是否明确指向器件、封装或订购总述。"""

    normalized = normalize_text(title)
    priority_title_terms = (
        "device information",
        "package information",
        "packaging information",
        "ordering information",
        "器件信息",
        "封装信息",
        "包装信息",
        "订购信息",
    )
    return any(term in normalized for term in priority_title_terms)


def _is_catalog_candidate_page(
    page_idx: int | None,
    *,
    document_page_count: int | None,
    toc_page_range: tuple[int, int] | None,
) -> bool:
    """按确认的目录和页码规则判断普通候选表所在页面。"""

    if not isinstance(page_idx, int) or page_idx < 0:
        return False
    if not isinstance(document_page_count, int) or document_page_count <= 0:
        return False

    # 最后十页始终属于候选区域；短文档自然会覆盖整篇文档。
    if page_idx >= max(0, document_page_count - 10):
        return True

    if toc_page_range is None:
        return page_idx < min(10, document_page_count)

    toc_start, toc_end = toc_page_range
    if page_idx < toc_start:
        return True
    return toc_end < page_idx <= min(toc_end + 3, document_page_count - 1)


def classify_package_catalog_candidates(
    tables: Sequence[PackageCatalogTable],
    *,
    source_name: str,
    target_tables: Sequence[PackageTargetTable],
    classifier: PackageCatalogClassifier | None = None,
) -> tuple[list[PackageCatalogEntry], list[dict[str, Any]]]:
    """并发判断表格结构，再由代码从原始行中建立封装目录。

    模型只能返回表格角色、表头行和列角色。pkg、封装类型、Drawing 和
    pin_count 的实际值都由本函数后续调用的确定性代码从 ``table.rows``
    读取，避免模型改写名称或把元数据误当成 pkg。
    """

    if not tables:
        return [], []
    # 生产路径使用四表批量协议。保留 classifier 注入仅供无网络单元测试，
    # 注入的旧式函数仍按单表调用，不改变既有测试接口。
    use_batch_classifier = classifier is None
    if use_batch_classifier:
        from extract.semantic_classifier import classify_package_catalog_tables

    import os

    workers = max(
        1,
        int(
            os.getenv(
                "EXTRACT_PACKAGE_WORKERS",
                os.getenv("EXTRACT_SCHEMA_WORKERS", "4"),
            )
        ),
    )
    batch_size = 4
    ordered_tables = list(enumerate(tables))
    batches = [
        ordered_tables[start:start + batch_size]
        for start in range(0, len(tables), batch_size)
    ]
    print(
        f"封装目录判断: 候选总述表 {len(tables)} 张, "
        f"每批最多 {batch_size} 张, 批次 {len(batches)}, "
        f"并发 {min(workers, len(batches))}",
        flush=True,
    )

    responses: list[tuple[int, PackageCatalogTable, Mapping[str, Any]]] = []
    diagnostics: list[dict[str, Any]] = []
    completed_tables = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as executor:
        if use_batch_classifier:
            futures = {
                executor.submit(
                    classify_package_catalog_tables,
                    [
                        (str(table.table_id), table)
                        for _, table in batch
                    ],
                    source_name=source_name,
                    target_tables=target_tables,
                ): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    batch_responses = future.result()
                except Exception as exc:
                    batch_responses = {}
                    batch_error = str(exc)
                else:
                    batch_error = ""
                for order, table in batch:
                    response = batch_responses.get(str(table.table_id))
                    if response is not None:
                        responses.append((order, table, response))
                    else:
                        diagnostics.append(
                            {
                                "stage": "package_catalog",
                                "table_id": table.table_id,
                                "status": "error",
                                "reason": (
                                    batch_error
                                    or "package_catalog_batch_missing_result"
                                ),
                            }
                        )
                    completed_tables += 1
                print(
                    f"封装目录判断进度: {completed_tables}/{len(tables)}",
                    flush=True,
                )
        else:
            futures = {
                executor.submit(
                    classifier,
                    table,
                    source_name,
                    target_tables,
                ): (order, table)
                for order, table in enumerate(tables)
            }
            for future in as_completed(futures):
                order, table = futures[future]
                try:
                    responses.append((order, table, future.result()))
                except Exception as exc:
                    diagnostics.append(
                        {
                            "stage": "package_catalog",
                            "table_id": table.table_id,
                            "status": "error",
                            "reason": str(exc),
                        }
                    )
                completed_tables += 1
                print(
                    f"封装目录判断进度: {completed_tables}/{len(tables)}",
                    flush=True,
                )

    decisions: list[tuple[int, PackageCatalogTable, PackageCatalogDecision]] = []
    # 模型并发完成顺序不可控；按 table_id 恢复全文原始表格顺序。
    for _, table, response in sorted(
        responses,
        key=lambda item: item[1].table_id,
    ):
        decision = package_catalog_decision_from_response(table, response)
        if not decision.is_package_summary:
            diagnostics.append(
                {
                    "stage": "package_catalog",
                    "table_id": table.table_id,
                    "status": "rejected",
                    "reason": "model_not_package_summary",
                }
            )
            continue
        decisions.append((table.table_id, table, decision))
        diagnostics.append(
            {
                "stage": "package_catalog",
                "table_id": table.table_id,
                "status": "structure_accepted",
                "table_role": decision.table_role,
                "header_row_index": decision.header_row_index,
                "columns": [
                    {
                        "column_index": column.column_index,
                        "role": column.role,
                    }
                    for column in decision.columns
                ],
            }
        )
    entries = build_catalog_entries_from_decisions(
        decisions,
        diagnostics,
        target_tables=target_tables,
    )
    return entries, diagnostics


def package_catalog_decision_from_response(
    table: PackageCatalogTable,
    response: Mapping[str, Any],
) -> PackageCatalogDecision:
    """把模型返回值转换成只含结构信息的内部对象。"""

    table_role = str(response.get("table_role") or "irrelevant")
    if table_role not in {
        "identity_summary",
        "packaging_metadata",
        "irrelevant",
    }:
        table_role = "irrelevant"
    try:
        header_row_index = int(response.get("header_row_index", 0))
    except (TypeError, ValueError):
        header_row_index = 0
    if not table.rows:
        header_row_index = 0
    else:
        header_row_index = min(max(header_row_index, 0), len(table.rows) - 1)

    allowed_roles = {
        "package_identity",
        "package_type",
        "package_drawing",
        "pin_count",
        "orderable_sku",
    }
    width = max((len(row) for row in table.rows), default=len(table.headers))
    columns: list[PackageColumnRole] = []
    seen: set[tuple[int, str]] = set()
    for item in response.get("columns") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            column_index = int(item.get("column_index"))
        except (TypeError, ValueError):
            continue
        role = str(item.get("role") or "")
        if role not in allowed_roles or column_index < 0 or column_index >= width:
            continue
        key = (column_index, role)
        if key in seen:
            continue
        seen.add(key)
        columns.append(
            PackageColumnRole(
                column_index=column_index,
                role=role,
                header=(
                    str(table.rows[header_row_index][column_index])
                    if (
                        table.rows
                        and column_index < len(table.rows[header_row_index])
                    )
                    else table.headers[column_index]
                    if column_index < len(table.headers)
                    else f"column_{column_index + 1}"
                ),
            )
        )

    # 只有身份总述表具有 package_identity，或者包装信息表具有
    # orderable_sku 时，结构判断才可进入确定性读取。
    roles = {column.role for column in columns}
    structurally_valid = (
        table_role == "identity_summary" and "package_identity" in roles
    ) or (
        table_role == "packaging_metadata" and "orderable_sku" in roles
    )
    raw_values: list[PackageCatalogRawValue] = []
    for item in response.get("entries") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            source_row_index = int(item.get("source_row_index", -1))
        except (TypeError, ValueError):
            source_row_index = -1
        raw_values.append(
            PackageCatalogRawValue(
                device=clean_identity_name(str(item.get("device") or "")),
                package=clean_public_package_name(str(item.get("package") or "")),
                drawing_code=clean_metadata(str(item.get("drawing_code") or "")),
                declared_pin_count=clean_pin_count(
                    str(item.get("declared_pin_count") or "")
                ),
                source_row_index=source_row_index,
            )
        )

    return PackageCatalogDecision(
        is_package_summary=(
            bool(response.get("is_package_summary")) and structurally_valid
        ),
        table_role=table_role if structurally_valid else "irrelevant",
        header_row_index=header_row_index,
        columns=tuple(columns),
        raw_values=tuple(raw_values),
    )


def build_catalog_entries_from_decisions(
    decisions: Sequence[
        tuple[int, PackageCatalogTable, PackageCatalogDecision]
    ],
    diagnostics: list[dict[str, Any]],
    *,
    target_tables: Sequence[PackageTargetTable] = (),
) -> list[PackageCatalogEntry]:
    """把模型确认的原始列转换为事实，再按严格身份键建立槽位。

    模型不拥有合并权限。代码逐行读取原始单元格；pin_count 只进入事实和
    调试信息。所有通过第二次模型确认并由原表复核的事实都参与建槽；模型
    返回的表格角色不能改变槽位数量，也不能让某类事实覆盖另一类事实。
    """

    facts = extract_raw_package_facts(decisions, diagnostics)
    slot_facts = facts
    entries = build_strict_package_slots(slot_facts)

    # 所有原始事实都附加到能够严格命中的槽位；未命中的事实仍保留在全局
    # diagnostics 中，不能靠 SKU 前缀、pin_count 或封装族相似度强行合并。
    for fact in facts:
        key = strict_package_fact_key(fact)
        if key is None:
            continue
        for entry in entries:
            if strict_entry_key(entry) == key and fact not in entry.raw_facts:
                entry.raw_facts.append(fact)
                if fact.source_table_id not in entry.evidence_table_ids:
                    entry.evidence_table_ids.append(fact.source_table_id)
                break

    diagnostics.append(
        {
            "stage": "package_slot_construction",
            "raw_fact_count": len(facts),
            "slot_fact_count": len(slot_facts),
            "slot_count": len(entries),
            "merge_rule": "complete_device_package_drawing_exact_match",
            "pin_count_used_for_merge": False,
        }
    )
    return entries


def extract_raw_package_facts(
    decisions: Sequence[
        tuple[int, PackageCatalogTable, PackageCatalogDecision]
    ],
    diagnostics: list[dict[str, Any]],
) -> list[RawPackageFact]:
    """按模型确认的列位置读取原表，保留每条封装事实及来源行。"""

    facts: list[RawPackageFact] = []
    for _, table, decision in decisions:
        table_facts: list[RawPackageFact] = []
        # 模型按协议返回原始值时，先逐项校验这些值确实存在于对应原表行；
        # 校验不通过的模型文本不能成为事实。随后仍由列映射读取原表，保证
        # 模型漏项不会造成目录事实静默消失。
        verified_model_values = {
            (
                value.device,
                value.package,
                value.drawing_code,
                value.declared_pin_count,
                value.source_row_index,
            )
            for value in decision.raw_values
            if package_raw_value_exists_in_source(table, value)
        }
        for row_index, row in enumerate(
            table.rows[decision.header_row_index + 1 :],
            start=decision.header_row_index + 1,
        ):
            device = first_role_value(row, decision, "package_identity")
            # orderable_sku 是订购料号，不是器件身份。包装表没有独立
            # package_identity 时 device 必须保持为空，后续也不能用 SKU
            # 前缀推断或补写 device。
            package = first_role_value(row, decision, "package_type")
            package, embedded_pin_count = split_package_type_and_pin_count(package)
            drawing = first_role_value(row, decision, "package_drawing")
            pin_count = (
                clean_pin_count(first_role_value(row, decision, "pin_count"))
                or embedded_pin_count
            )
            fact = RawPackageFact(
                device=clean_identity_name(device),
                package=clean_public_package_name(package),
                drawing_code=clean_metadata(drawing),
                declared_pin_count=pin_count,
                source_table_id=table.table_id,
                source_row_index=row_index,
                table_role=decision.table_role,
            )
            # 空行、重复表头和只有 pin_count 的行都不是封装事实。
            if not (fact.device or fact.package or fact.drawing_code):
                continue
            if any(
                is_repeated_header_value(value, column.header)
                for value, role in (
                    (fact.device, "package_identity"),
                    (fact.package, "package_type"),
                    (fact.drawing_code, "package_drawing"),
                )
                for column in columns_for_role(decision, role)
                if value
            ):
                continue
            facts.append(fact)
            table_facts.append(fact)
        diagnostics.append(
            {
                "stage": "package_catalog_raw_facts",
                "table_id": table.table_id,
                "table_role": decision.table_role,
                "facts": [raw_package_fact_to_dict(fact) for fact in table_facts],
                "verified_model_value_count": len(verified_model_values),
            }
        )
    return facts


def package_raw_value_exists_in_source(
    table: PackageCatalogTable,
    value: PackageCatalogRawValue,
) -> bool:
    """验证模型抄录值来自指定原表行，防止模型生成不存在的封装。"""

    if value.source_row_index < 0 or value.source_row_index >= len(table.rows):
        return False
    row_text = normalize_compact(" ".join(table.rows[value.source_row_index]))
    evidence = [
        value.device,
        value.package,
        value.drawing_code,
        value.declared_pin_count,
    ]
    nonempty = [normalize_compact(item) for item in evidence if item]
    return bool(nonempty and all(item in row_text for item in nonempty))


def build_strict_package_slots(
    facts: Sequence[RawPackageFact],
) -> list[PackageCatalogEntry]:
    """按完整三元组建立槽位；任何字段缺失时都不主动与其他事实合并。"""

    entries: list[PackageCatalogEntry] = []
    keyed_entries: dict[tuple[str, str, str], PackageCatalogEntry] = {}
    for fact in facts:
        key = strict_package_fact_key(fact)
        if key is not None and key in keyed_entries:
            entry = keyed_entries[key]
            entry.raw_facts.append(fact)
            if fact.source_table_id not in entry.evidence_table_ids:
                entry.evidence_table_ids.append(fact.source_table_id)
            continue

        entry = PackageCatalogEntry(
            package_key="",
            identity_name=fact.device,
            package_type=fact.package,
            package_drawing=fact.drawing_code,
            pin_count=fact.declared_pin_count,
            evidence_table_ids=[fact.source_table_id],
            raw_facts=[fact],
            origin="package_catalog",
        )
        entries.append(entry)
        if key is not None:
            keyed_entries[key] = entry
    return entries


def strict_package_fact_key(
    fact: RawPackageFact,
) -> tuple[str, str, str] | None:
    """完整 device/package/drawing 才生成可合并键，pin_count 不参与。"""

    values = (
        normalize_compact(fact.device),
        normalize_compact(fact.package),
        normalize_compact(fact.drawing_code),
    )
    return values if all(values) else None


def strict_entry_key(
    entry: PackageCatalogEntry,
) -> tuple[str, str, str] | None:
    """返回槽位的严格身份键，供原始事实附加和测试使用。"""

    return strict_package_fact_key(
        RawPackageFact(
            device=entry.identity_name,
            package=entry.package_type,
            drawing_code=entry.package_drawing,
        )
    )


def raw_package_fact_to_dict(fact: RawPackageFact) -> dict[str, Any]:
    """将事实转换为稳定调试结构，不改变任何原始字段语义。"""

    return {
        "device": fact.device,
        "package": fact.package,
        "drawing_code": fact.drawing_code,
        "declared_pin_count": fact.declared_pin_count,
        "source_table_id": fact.source_table_id,
        "source_row_index": fact.source_row_index,
        "table_role": fact.table_role,
    }




def columns_for_role(
    decision: PackageCatalogDecision,
    role: str,
) -> list[PackageColumnRole]:
    """按模型确认的角色读取列，保持原表从左到右顺序。"""

    return [column for column in decision.columns if column.role == role]


def cell_value(
    row: Sequence[str],
    column: PackageColumnRole,
) -> str:
    """按列索引读取原始单元格并做最小空白清理。"""

    if column.column_index >= len(row):
        return ""
    return clean_metadata(str(row[column.column_index]))


def first_role_value(
    row: Sequence[str],
    decision: PackageCatalogDecision,
    role: str,
) -> str:
    """读取一行中第一个非空的指定角色值。"""

    for column in columns_for_role(decision, role):
        value = cell_value(row, column)
        if value and not is_repeated_header_value(value, column.header):
            return value
    return ""


def is_repeated_header_value(value: str, header: str) -> bool:
    """防止多段表格中重复出现的表头被当成 pkg 或元数据。"""

    normalized_value = normalize_text(value)
    normalized_header = normalize_text(header)
    return bool(
        normalized_value
        and normalized_header
        and normalized_value == normalized_header
    )


def split_package_type_and_pin_count(value: str) -> tuple[str, str]:
    """拆解 ``SC-70 (5)`` 这类同格元数据，不改变 pkg 身份。"""

    value = clean_metadata(value)
    match = re.fullmatch(r"(.*?)\s*\(\s*(\d+)\s*\)", value)
    if not match:
        return value, ""
    return clean_metadata(match.group(1)), match.group(2)




def plan_creates_package_slots(plan: MultiPackagePlanLike | None) -> bool:
    """判断单表分支是否代表独立物理封装槽位。

    ``parallel_name_columns`` 只是同一封装的运行模式名称列。它仍使用多分支
    行读取器生成记录，但不能参与文档级 pkg 数量和一对一封装绑定。
    """

    return bool(
        plan is not None
        and plan.is_multi_package
        and plan.mode != "parallel_name_columns"
    )


def _entries_matching_exact_branch_label(
    entries: Sequence[PackageCatalogEntry],
    label: str,
) -> list[PackageCatalogEntry]:
    """按器件身份、封装类型、内部别名或 Drawing 精确匹配表头分支。"""

    normalized_label = normalize_compact(label)
    if not normalized_label:
        return []
    return [
        entry
        for entry in entries
        if any(
            normalize_compact(value) == normalized_label
            for value in (
                entry.identity_name,
                entry.package_type,
                *entry.identity_aliases,
                entry.package_drawing,
            )
        )
    ]


def _append_internal_binding_alias(
    entry: PackageCatalogEntry,
    label: str,
) -> None:
    """保存表头分支标签作为内部绑定证据，不修改公开 package_type。"""

    label = str(label or "").strip()
    if (
        label
        and label != entry.identity_name
        and label not in entry.identity_aliases
    ):
        entry.identity_aliases.append(label)


def supplement_slots_from_explicit_table_branches(
    entries: Sequence[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    diagnostics: list[dict[str, Any]],
) -> list[PackageCatalogEntry]:
    """用明确的表头封装分支补全目录漏项，不读取任何正式数据值。

    只有 ``package_columns`` 和 ``package_name_columns`` 表示物理封装分支；
    运行模式、按行选择器和普通单封装表都不能扩张文档槽位。
    """

    result = list(entries)
    table_by_id = {table.table_id: table for table in target_tables}
    for table_id, plan in multi_package_plans.items():
        if not (
            plan is not None
            and plan.is_multi_package
            and plan.mode in {"package_columns", "package_name_columns"}
        ):
            continue
        table = table_by_id.get(table_id)
        for binding in plan.bindings:
            label = clean_explicit_branch_label(binding.package)
            if not label:
                continue
            matches = _entries_matching_exact_branch_label(result, label)
            if len(matches) == 1:
                _append_internal_binding_alias(matches[0], label)
                continue
            if len(matches) > 1:
                diagnostics.append(
                    {
                        "stage": "package_catalog_conflict",
                        "status": "ambiguous_existing_branch",
                        "reason": "catalog_conflict",
                        "table_id": table_id,
                        "branch_label": label,
                        "matched_entry_count": len(matches),
                    }
                )
                continue

            # 标签直接来自已经确认的逐列父表头。创建的槽位保留该标签作为
            # drawing/显示证据；它不是从数据行或 description 猜出来的。
            fact = RawPackageFact(
                drawing_code=label,
                source_table_id=table_id,
                source_row_index=-1,
                table_role="explicit_pin_table_branch",
            )
            entry = PackageCatalogEntry(
                package_key="",
                identity_aliases=[label],
                package_drawing=label,
                evidence_table_ids=[table_id],
                raw_facts=[fact],
                origin="explicit_pin_table_branch",
            )
            result.append(entry)
            diagnostics.append(
                {
                    "stage": "package_catalog_conflict",
                    "status": "supplemented_missing_slot",
                    "reason": "catalog_conflict",
                    "table_id": table_id,
                    "table_title": table.title if table is not None else "",
                    "branch_label": label,
                }
            )
    return result


def clean_explicit_branch_label(value: str) -> str:
    """清理结构分析得到的分支标签；拒绝空白和明显的长描述。"""

    label = re.sub(r"<[^>]+>", " ", str(value or ""))
    label = re.sub(r"\s+", " ", label).strip(" ,;:")
    # 多层表头经路径拼接后常形成 ``SSOP 28 PIN``、``QFN 32 BALL``。
    # 末尾的字段角色不属于封装名称，移除后仍保留封装族和数字规格。
    label = re.sub(
        r"\s+(?:PIN|PINS|BALL|BALLS)(?:\s*(?:NO\.?|NUMBER))?$",
        "",
        label,
        flags=re.IGNORECASE,
    ).strip(" ,;:")
    if not label or len(label) > 40 or "|" in label:
        return ""
    return label


def freeze_package_slots(entries: Sequence[PackageCatalogEntry]) -> None:
    """按首次出现顺序冻结槽位 key；名称变化不能改变分组身份。"""

    for slot_index, entry in enumerate(entries):
        entry.package_key = f"slot:{slot_index}"


def bind_target_tables(
    *,
    entries: Sequence[PackageCatalogEntry],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    multi_pkg_tab_resolution: MultiPkgTabResolution | None = None,
    diagnostics: list[dict[str, Any]],
) -> dict[tuple[int, int], PackageAssignment]:
    """把每张目标表绑定到已冻结槽位，不允许在此阶段创建新槽位。"""

    assignments: dict[tuple[int, int], PackageAssignment] = {}
    previous_explicit: PackageAssignment | None = None
    previous_context = ""

    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        local_labels = (
            [binding.package for binding in plan.bindings]
            if plan is not None and plan.is_multi_package
            else [""]
        )

        explicit_matches, explicit_reason = match_target_table_context(
            entries,
            table,
        )
        # 只有文档已经形成至少两个可验证跨表分支时，新模块才接管绑定。
        # 单个局部分支仍沿用原来的表题/表头匹配，避免把普通单封装表误标成
        # cross_table_package_branch，也保证原有单封装路径完全不变。
        cross_table_branch = (
            multi_pkg_tab_resolution.branch_for_table(table.table_id)
            if (
                multi_pkg_tab_resolution is not None
                and multi_pkg_tab_resolution.document_mode
                in {"cross_table_multi_package", "mixed_multi_package"}
            )
            else None
        )
        if cross_table_branch is not None:
            branch_matches = _entries_matching_exact_branch_label(
                entries,
                cross_table_branch.label,
            )
            # 新模块已经根据表题/表头/最近章节建立了唯一跨表分支。这里只
            # 接受唯一目录槽位；若目录仍有同名歧义，则保持 unresolved。
            explicit_matches = branch_matches
            explicit_reason = "cross_table_package_branch"

        # 运行模式名称列需要分别生成 pin 记录，但所有分支属于当前表的同一
        # 物理封装。这里先按表题/最近章节确定一次槽位，再把同一绑定复制给
        # 每个本地名称分支，禁止进入多封装一对一匹配。
        if (
            plan is not None
            and plan.mode == "parallel_name_columns"
            and len(local_labels) >= 2
        ):
            assignment: PackageAssignment | None
            if len(explicit_matches) == 1:
                assignment = assignment_from_entry(
                    explicit_matches[0],
                    entries,
                    reason=explicit_reason,
                )
            elif len(entries) == 1:
                assignment = assignment_from_entry(
                    entries[0],
                    entries,
                    reason="single_document_package",
                )
            elif (
                not explicit_matches
                and previous_explicit is not None
                and previous_context == chapter_context_key(table)
            ):
                assignment = PackageAssignment(
                    previous_explicit.package_key,
                    previous_explicit.pkg,
                    "same_chapter_continuation",
                )
            else:
                assignment = None

            unresolved_reason = (
                "ambiguous_package_evidence"
                if len(explicit_matches) > 1
                else "package_unresolved"
            )
            for local_slot, local_label in enumerate(local_labels):
                append_package_binding_diagnostic(
                    diagnostics,
                    table=table,
                    local_slot=local_slot,
                    local_label=local_label,
                    assignment=assignment,
                    reason=(assignment.reason if assignment else unresolved_reason),
                    matched_entries=explicit_matches,
                    entries=entries,
                )
                if assignment is not None:
                    assignments[(table.table_id, local_slot)] = assignment
            if assignment is not None and assignment.reason in {
                "cross_table_package_branch",
                "table_title_or_header",
                "table_group_context",
                "nearest_chapter_title",
            }:
                previous_explicit = assignment
                previous_context = chapter_context_key(table)
            elif chapter_context_key(table) != previous_context:
                previous_explicit = None
                previous_context = ""
            continue

        # 多封装表必须把全部分支作为一个整体绑定。若逐个分支独立匹配，两个
        # 模糊标签可能同时落入第一个槽位，导致本应分开的 pkg 再次合并。
        if plan_creates_package_slots(plan) and len(local_labels) >= 2:
            bound_entries = bind_multi_package_entries(entries, local_labels)
            if not bound_entries:
                # 多分支整体绑定必须同时具有完整且唯一的证据。任一分支
                # 无法确定时整张表保持 unresolved，不能按目录位置硬配。
                for local_slot, local_label in enumerate(local_labels):
                    append_package_binding_diagnostic(
                        diagnostics,
                        table=table,
                        local_slot=local_slot,
                        local_label=local_label,
                        assignment=None,
                        reason="package_unresolved",
                        matched_entries=match_entries_in_text(entries, local_label),
                        entries=entries,
                    )
                previous_explicit = None
                previous_context = ""
                continue
            for local_slot, (local_label, entry) in enumerate(
                zip(local_labels, bound_entries)
            ):
                assignment = assignment_from_entry(
                    entry,
                    entries,
                    reason="multi_package_global_unique_binding",
                )
                assignments[(table.table_id, local_slot)] = assignment
                diagnostics.append(
                    {
                        "stage": "package_binding",
                        "table_id": table.table_id,
                        "local_slot": local_slot,
                        "local_label": local_label,
                        "package_key": assignment.package_key,
                        "pkg": assignment.pkg,
                        "reason": assignment.reason,
                    }
                )
            # 多分支表不能成为单封装续表的继承来源。
            if chapter_context_key(table) != previous_context:
                previous_explicit = None
                previous_context = ""
            continue

        for local_slot, local_label in enumerate(local_labels):
            local_matches = match_entries_in_text(entries, local_label)
            matches = local_matches or explicit_matches
            reason = ""

            if len(matches) == 1:
                entry = matches[0]
                reason = (
                    "multi_package_binding_label"
                    if local_matches
                    else explicit_reason
                )
                assignment = assignment_from_entry(
                    entry,
                    entries,
                    reason=reason,
                )
            elif len(entries) == 1:
                entry = entries[0]
                assignment = assignment_from_entry(
                    entry,
                    entries,
                    reason="single_document_package",
                )
            elif (
                not matches
                and previous_explicit is not None
                and previous_context
                and previous_context == chapter_context_key(table)
            ):
                # 同一章节中，明确封装表之后的无标记续表可以继承。跨章节禁止
                # 继承，避免把下一个器件/封装章节归到上一封装。
                assignment = PackageAssignment(
                    previous_explicit.package_key,
                    previous_explicit.pkg,
                    "same_chapter_continuation",
                )
            else:
                # 多封装文档中，没有唯一归属证据的表不能再默认绑定第一个
                # pkg。这里故意不创建 assignment，让主提取器整表跳过。
                assignment = None

            unresolved_reason = (
                "ambiguous_package_evidence"
                if len(matches) > 1
                else "package_unresolved"
            )
            append_package_binding_diagnostic(
                diagnostics,
                table=table,
                local_slot=local_slot,
                local_label=local_label,
                assignment=assignment,
                reason=(assignment.reason if assignment else unresolved_reason),
                matched_entries=matches,
                entries=entries,
            )
            if assignment is not None:
                assignments[(table.table_id, local_slot)] = assignment

        # 只有单一且基于当前表明确文字命中的结果，才允许成为后续续表来源。
        first_assignment = assignments.get((table.table_id, 0))
        if first_assignment is not None and first_assignment.reason in {
            "cross_table_package_branch",
            "multi_package_binding_label",
            "table_title_or_header",
            "table_group_context",
            "nearest_chapter_title",
        }:
            previous_explicit = first_assignment
            previous_context = chapter_context_key(table)
        elif chapter_context_key(table) != previous_context:
            previous_explicit = None
            previous_context = ""
    return assignments


def append_package_binding_diagnostic(
    diagnostics: list[dict[str, Any]],
    *,
    table: PackageTargetTable,
    local_slot: int,
    local_label: str,
    assignment: PackageAssignment | None,
    reason: str,
    matched_entries: Sequence[PackageCatalogEntry],
    entries: Sequence[PackageCatalogEntry],
) -> None:
    """记录一次绑定结果；unresolved 只写诊断，不伪造 pkg assignment。"""

    diagnostic = {
        "stage": "package_binding",
        "status": "resolved" if assignment is not None else "unresolved",
        "table_id": table.table_id,
        "local_slot": local_slot,
        "local_label": local_label,
        "reason": reason,
        "matched_packages": [
            assignment_from_entry(entry, entries, reason="diagnostic").pkg
            for entry in matched_entries
        ],
        "document_packages": [
            assignment_from_entry(entry, entries, reason="diagnostic").pkg
            for entry in entries
        ],
    }
    if assignment is not None:
        diagnostic["package_key"] = assignment.package_key
        diagnostic["pkg"] = assignment.pkg
    diagnostics.append(diagnostic)


def bind_multi_package_entries(
    entries: Sequence[PackageCatalogEntry],
    local_labels: Sequence[str],
) -> list[PackageCatalogEntry]:
    """把一张表的全部本地分支绑定到互不重复且证据唯一的目录槽位。

    这里只比较器件身份、封装名称和 drawing。声明 pin 数量及列位置都不
    参与绑定。任一分支没有正向证据、存在并列最高分或多个分支指向同一
    槽位时返回空列表，由调用方把整张表记录为 unresolved。
    """

    if len(entries) < len(local_labels):
        return []

    score_matrix = [
        [
            multi_package_binding_score(label, entry)
            for entry_slot, entry in enumerate(entries)
        ]
        for local_slot, label in enumerate(local_labels)
    ]

    # 每个分支都必须有且只有一个最高分槽位。先做逐分支唯一性校验，避免
    # 匈牙利算法在同分或全零矩阵中人为制造一个看似完整的绑定。
    unique_slots: list[int] = []
    for scores in score_matrix:
        best_score = max(scores, default=0)
        if best_score <= 0:
            return []
        best_slots = [index for index, score in enumerate(scores) if score == best_score]
        if len(best_slots) != 1:
            return []
        unique_slots.append(best_slots[0])
    if len(set(unique_slots)) != len(unique_slots):
        return []

    selected_slots = maximum_weight_unique_assignment(score_matrix)
    if selected_slots != unique_slots:
        return []
    return [entries[entry_slot] for entry_slot in selected_slots]


def maximum_weight_unique_assignment(
    score_matrix: Sequence[Sequence[int]],
) -> list[int]:
    """用矩形匈牙利算法求每个分支对应的唯一目录槽位。

    行是表内分支，列是文档目录槽位，且行数不大于列数。算法复杂度为
    ``O(分支数^2 * 槽位数)``，不会随着目录候选增加而进行组合枚举。
    """

    row_count = len(score_matrix)
    if row_count == 0:
        return []
    column_count = len(score_matrix[0])
    if column_count < row_count:
        raise ValueError("unique assignment requires at least one slot per branch")
    if any(len(row) != column_count for row in score_matrix):
        raise ValueError("assignment score matrix must be rectangular")

    # 标准算法求最小代价，因此把最大得分转换为相反数。下标 0 是哨兵位。
    row_potential = [0] * (row_count + 1)
    column_potential = [0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)

    for row_number in range(1, row_count + 1):
        matched_row[0] = row_number
        current_column = 0
        minimum_cost = [float("inf")] * (column_count + 1)
        used_column = [False] * (column_count + 1)
        while True:
            used_column[current_column] = True
            current_row = matched_row[current_column]
            delta = float("inf")
            next_column = 0
            for column_number in range(1, column_count + 1):
                if used_column[column_number]:
                    continue
                reduced_cost = (
                    -score_matrix[current_row - 1][column_number - 1]
                    - row_potential[current_row]
                    - column_potential[column_number]
                )
                if reduced_cost < minimum_cost[column_number]:
                    minimum_cost[column_number] = reduced_cost
                    previous_column[column_number] = current_column
                if minimum_cost[column_number] < delta:
                    delta = minimum_cost[column_number]
                    next_column = column_number
            for column_number in range(column_count + 1):
                if used_column[column_number]:
                    row_potential[matched_row[column_number]] += delta
                    column_potential[column_number] -= delta
                else:
                    minimum_cost[column_number] -= delta
            current_column = next_column
            if matched_row[current_column] == 0:
                break

        while True:
            predecessor = previous_column[current_column]
            matched_row[current_column] = matched_row[predecessor]
            current_column = predecessor
            if current_column == 0:
                break

    selected_slots = [-1] * row_count
    for column_number in range(1, column_count + 1):
        if matched_row[column_number]:
            selected_slots[matched_row[column_number] - 1] = column_number - 1
    if any(slot < 0 for slot in selected_slots):
        raise RuntimeError("failed to bind every package branch to a unique slot")
    return selected_slots


def multi_package_binding_score(
    local_label: str,
    entry: PackageCatalogEntry,
) -> int:
    """计算一个分支标签与一个目录槽位之间的确定性关联分数。"""

    label_text = normalize_text(local_label)
    label_compact = normalize_compact(local_label)
    score = 0

    identities = [entry.identity_name, *entry.identity_aliases]
    for identity in identities:
        identity_compact = normalize_compact(clean_identity_name(identity))
        if not identity_compact:
            continue
        if identity_compact == label_compact:
            score = max(score, 1200)
        elif package_name_in_text(identity, label_text):
            score = max(score, 950)

    package_type = clean_public_package_name(entry.package_type)
    package_type_compact = normalize_compact(package_type)
    if package_type_compact:
        if package_type_compact == label_compact:
            score += 700
        elif package_name_in_text(package_type, label_text):
            score += 350

    drawing = clean_metadata(entry.package_drawing)
    drawing_compact = normalize_compact(drawing)
    if drawing_compact:
        if drawing_compact == label_compact:
            score += 450
        elif package_name_in_text(drawing, label_text):
            score += 300

    return score




def match_entries_in_text(
    entries: Sequence[PackageCatalogEntry],
    text: str,
) -> list[PackageCatalogEntry]:
    """按完整名称边界匹配目录，不执行编辑距离或前缀猜测。"""

    normalized_text = normalize_text(text)
    matches = []
    for entry in entries:
        # 器件型号只用于内部关联；封装类型和 Drawing 也可参与绑定。若同一
        # 元数据对应多个槽位，会保持歧义，不能据此合并槽位。
        names = [entry.identity_name, *entry.identity_aliases]
        if entry.package_type:
            names.append(entry.package_type)
        if entry.package_drawing:
            names.append(entry.package_drawing)
        if any(package_name_in_text(name, normalized_text) for name in names):
            matches.append(entry)
    return matches


def package_name_in_text(name: str, normalized_text: str) -> bool:
    """确保短名称不会命中另一个更长标识符的内部片段。"""

    normalized_name = normalize_text(name)
    if not normalized_name:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_name)}(?![a-z0-9])",
            normalized_text,
        )
    )


def match_target_table_context(
    entries: Sequence[PackageCatalogEntry],
    table: PackageTargetTable,
) -> tuple[list[PackageCatalogEntry], str]:
    """按局部证据强弱匹配目标表所属封装。

    表题最强；随后检查表格附近上下文，再从离当前表最近的章节标题向前
    查找，最后检查表头。不能把全部历史章节标题先拼接，因为其中可能同时
    包含前后两个器件身份，导致本可确定的 SF2507/SF2507E 表被判成歧义。
    """

    checks: list[tuple[str, str]] = [("table_title_or_header", table.title)]
    checks.append(("table_group_context", table.group_context))
    checks.extend(
        ("nearest_chapter_title", title)
        for title in reversed(table.current_chapter_titles)
    )
    checks.append(("table_title_or_header", " ".join(table.headers)))

    first_ambiguous: tuple[list[PackageCatalogEntry], str] | None = None
    seen_texts: set[str] = set()
    for reason, text in checks:
        normalized = normalize_text(text)
        if not normalized or normalized in seen_texts:
            continue
        seen_texts.add(normalized)
        matches = match_entries_in_text(entries, text)
        if len(matches) == 1:
            return matches, reason
        if len(matches) > 1 and first_ambiguous is None:
            first_ambiguous = (matches, reason)

    return first_ambiguous or ([], "")


def chapter_context_key(table: PackageTargetTable) -> str:
    """生成续表继承使用的当前章节键。"""

    return normalize_text("\n".join(table.current_chapter_titles))


def assignment_from_entry(
    entry: PackageCatalogEntry,
    entries: Sequence[PackageCatalogEntry],
    *,
    reason: str,
) -> PackageAssignment:
    """把内部槽位转换为表绑定；器件型号绝不能进入公开 pkg。"""

    slot_index = next(
        (
            index
            for index, candidate in enumerate(entries)
            if candidate.package_key == entry.package_key
        ),
        None,
    )
    if slot_index is None:
        raise ValueError(
            f"package entry is not present in the frozen catalog: {entry.package_key}"
        )
    return PackageAssignment(
        package_key=entry.package_key,
        pkg=(
            clean_metadata(entry.package_drawing)
            or clean_public_package_name(entry.package_type)
            or alphabetic_slot_name(slot_index)
        ),
        reason=reason,
    )


def alphabetic_slot_name(slot_index: int) -> str:
    """把 0、1、2……稳定转换成 a、b、c……、aa、ab……。"""

    value = max(0, int(slot_index))
    result = ""
    while True:
        value, remainder = divmod(value, 26)
        result = chr(ord("a") + remainder) + result
        if value == 0:
            return result
        value -= 1


def clean_identity_name(value: str) -> str:
    """清理器件型号；该值只用于内部关联，永远不直接输出。"""

    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,;:")
    # 表头脚注不是型号的一部分，例如 DRV8311S(2) 应与 DRV8311S 对应。
    value = re.sub(r"\s*[\(（]\s*\d+\s*[\)）]\s*$", "", value)
    if not value or "|" in value or "\n" in value or len(value) > 15:
        return ""
    return value


def clean_public_package_name(value: str) -> str:
    """清理公开物理封装名，拒绝拼接结果和长描述。"""

    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,;:")
    # 订购表常把尺寸、物理封装和环保信息写在同一格，例如
    # ``14 mm x 14 mm LQFP-128 E-PAD (Pb-Free)``。公开 pkg 只截取其中
    # 明确的封装族片段，不能因为整格超过 15 字符就丢失真实封装名。
    embedded_package = extract_public_package_name_from_metadata(value)
    if embedded_package:
        value = embedded_package
    # ``SSOP 28 Pin`` 中末尾 Pin 是列角色；保留 SSOP 28 作为封装名。
    value = re.sub(r"\s+\bpin\b\s*$", "", value, flags=re.IGNORECASE)
    if (
        not value
        or "|" in value
        or "\n" in value
        or len(value) > 15
        or is_generic_package_label(value)
    ):
        return ""
    return value


def extract_public_package_name_from_metadata(value: str) -> str:
    """从长包装描述中截取明确的物理封装族名称。

    这里只识别数据手册常见封装族及其紧邻的编号/E-PAD 后缀。尺寸、器件型号、
    Pb-Free 等其余文字都不会进入公开 pkg，也不会参与槽位数量判断。
    """

    family = (
        r"(?:HTSSOP|TSSOP|VSSOP|SSOP|HTQFP|TQFP|LQFP|QFP|"
        r"VQFN|WQFN|QFN|DFN|SON|X2SON|HSBGA|LFBGA|FBGA|PBGA|"
        r"DSBGA|BGA|WCSP|CSP|SOIC|MSOP|SOT|SC)"
    )
    match = re.search(
        rf"(?<![A-Za-z0-9])({family}(?:[- ]\d+)?(?:\s+E-?PAD)?)(?![A-Za-z0-9])",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()




def catalog_header_hints(rows: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """合并表格前四行的逐列文字，供总述表宽松定位使用。

    MinerU 的多级表头可能把第一行解析成跨列表题，真正的 Package/Device
    字段位于第二或第三行。这里只形成定位提示，不改变原始 ``rows``。
    """

    if not rows:
        return ()
    width = max((len(row) for row in rows[:4]), default=0)
    hints = []
    for column_index in range(width):
        values = []
        for row in rows[:4]:
            value = str(row[column_index]).strip() if column_index < len(row) else ""
            if value and value not in values:
                values.append(value)
        hints.append(" ".join(values))
    return tuple(hints)


def clean_metadata(value: str) -> str:
    """清理只用于调试和消歧的封装元数据。"""

    return re.sub(r"\s+", " ", value).strip()


def clean_pin_count(value: Any) -> str:
    """把原始表格中的 pin_count 规范成数字字符串。"""

    match = re.search(r"\d+", str(value or ""))
    return match.group(0) if match else ""


def is_generic_package_label(value: str) -> bool:
    """排除只能表示表格角色、不能表示真实封装身份的标签。"""

    normalized = normalize_text(value)
    return normalized in {
        "package",
        "packages",
        "package pin",
        "pin",
        "pin no",
        "pin number",
        "ball",
        "ball no",
        "terminal",
    }


def normalize_text(value: str) -> str:
    """保留词边界的通用比较文本。"""

    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_compact(value: str) -> str:
    """去除分隔符，用于核对名称是否真实出现在证据文本中。"""

    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())
