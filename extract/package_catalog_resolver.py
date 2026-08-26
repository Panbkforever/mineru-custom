"""建立文档级物理封装槽位，并把已确认的引脚表绑定到槽位。

本模块位于“表格/字段判断”之后、“逐行生成引脚记录”之前。它只处理
最终 JSON 最外层的 ``pkg``，不判断 pin_no、pin_name、type，也不修改
任何表格行内容。

固定处理流程：

1. 从全文表格中定位可能的封装总述表或包装信息表。当前表自己的表题命中
   器件信息、封装信息、订购信息及对应英文时属于最高优先级；否则只从
   目录前、目录结束后三页或文档末十页等限定页面区域召回。章节标题不能
   代替当前表题触发最高优先级。
2. 模型只判断表格角色、表头行和列角色，不返回任何 pkg 值。
3. 代码按照模型给出的列索引逐行读取原表，并结合已经严格确认的表内分支，
   确定文档中有几个相互独立的物理引脚映射空间，再冻结为
   slot:0、slot:1……。
4. ``package_identity``（器件型号）只作为跨表关联证据；公开 ``pkg`` 默认取
   ``package_type``（SC-70、VSSOP、QFN 等物理封装名称）。只有目标引脚表的
   表题/表头/跨表分支明确确认了短 symbol/drawing 时，才会使用该短标签作为
   public label。
5. 严格确认的 N 个表内分支是 N 个独立输出槽位的下限。总述表即使只找到
   一个公开封装名，也必须建立 N 个槽位并复用该名称；最终输出再追加数字
   后缀。已有严格多分支证据时，只有器件型号、没有任何物理封装元数据的
   目录项不能额外增加槽位。没有多分支证据时，仍保持原目录判断流程。
6. 目标引脚表只通过表题、章节标题、表头和多封装列标签绑定已有槽位；
   description 和数据行不能参与绑定。
7. 槽位冻结后，任何未匹配表都不能创建新 pkg。单封装文档可以绑定唯一
   槽位；多封装文档中无法唯一归属的表必须标记为 unresolved，禁止默认
   塞入第一个槽位。若多个目录槽位全部无法绑定、没有真实多封装分支证据，
   且目标表局部上下文没有多目标封装证据，才按单封装误膨胀兜底收敛为
   一个槽位。真实名称缺失时仅对已经确认的槽位使用 a、b、c……。
8. 多封装表的全部本地分支必须一次性执行一对一绑定；禁止每个分支独立
   兜底后落入同一个 package_key。标签脚注、drawing 和 pin_count 只用于
   内部消歧，不改变任何引脚行内容。
9. ``XXX Mode Pin Name`` 形成的运行模式分支只控制名称列读取，不创建 pkg
   槽位；这些分支必须共同绑定当前表所属的同一个物理封装。

特别重要的边界：

* 这里不会再次调用引脚表字段判断，也不会生成引脚记录。
* ``multi_package_extractor.py`` 仍只负责单张表内部的多封装结构。
* description 和普通正文不能参与封装绑定。
* 一个 pkg 只能是一个字符串，禁止使用 ``|`` 拼接多个候选名称。
* pkg 名称最长 15 个字符；超过长度的标题、描述或多个名称拼接结果直接拒绝。
* 器件型号、订购型号和未经目标引脚表确认的 Drawing 不能写入公开 pkg。
* 不能为每张未匹配表生成 ``unresolved:table_id``，否则表数会被误当成封装数。
* 两个槽位即使公开封装名相同也保持独立；只有建立槽位时的同一行/同一结构
  证据才能决定它们是不是同一个物理映射空间。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from extract.multi_PkgTab_extractor import (
    MultiPkgTabResolution,
    catalog_groups_confirmed_by_target_tables,
    resolve_multi_pkg_tab_structure,
)


PACKAGE_FAMILY_PATTERN = (
    r"(?:HTSSOP|HVSSOP|TSSOP|VSSOP|SSOP|HTQFP|TQFP|LQFP|QFP|"
    r"VQFN|WQFN|QFN|DFN|SON|X2SON|HSBGA|NFBGA|LFBGA|FBGA|PBGA|"
    r"DSBGA|BGA|FCCSP|FCBGA|WCSP|CSP|SOIC|MSOP|SOT|SC)"
)

PHYSICAL_PACKAGE_LABEL_COMPACT_RE = re.compile(
    r"^(?:htssop|hvssop|tssop|vssop|ssop|htqfp|tqfp|lqfp|qfp|"
    r"vqfn|wqfn|qfn|dfn|son|x2son|hsbga|nfbga|lfbga|fbga|pbga|"
    r"dsbga|bga|fccsp|fcbga|wcsp|csp|soic|msop|sot|sc)(?:\d+)?$"
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


@dataclass
class PackageCatalogEntry:
    """一个已经冻结的物理封装槽位。

    ``identity_name`` 是器件型号，只参与表格关联；``package_type`` 是物理封装
    名称；``public_label`` 只保存目标引脚表已确认的短 symbol/drawing。
    """

    package_key: str
    identity_name: str = ""
    identity_aliases: list[str] = field(default_factory=list)
    package_type: str = ""
    package_drawing: str = ""
    pin_count: str = ""
    evidence_table_ids: list[int] = field(default_factory=list)
    public_label: str = ""


@dataclass(frozen=True)
class PackageColumnRole:
    """模型确认的一列语义；这里只保存列索引和角色，不保存单元格值。"""

    column_index: int
    role: str
    header: str = ""


@dataclass(frozen=True)
class PackageCatalogDecision:
    """模型对一张候选表的结构判断。"""

    is_package_summary: bool
    table_role: str
    header_row_index: int
    columns: tuple[PackageColumnRole, ...] = ()


@dataclass(frozen=True)
class PackageAssignment:
    """目标表的一个本地封装槽位对应的文档级封装。"""

    package_key: str
    pkg: str
    reason: str


@dataclass(frozen=True)
class SymbolPackageLinkResolution:
    """表题/表头中的 Symbol(Package) must-link 解析结果。"""

    effective_labels: tuple[str, ...]
    sources_by_slot: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    conflicts: tuple[dict[str, Any], ...] = ()


@dataclass
class PackageCatalogResolution:
    """整篇文档的封装目录、绑定结果和调试信息。"""

    entries: list[PackageCatalogEntry]
    assignments: dict[tuple[int, int], PackageAssignment]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

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
            source_name="",
            target_tables=target_tables,
            classifier=classifier,
        )
        diagnostics.extend(semantic_diagnostics)

    # 同一物理封装有时会被总述表和 Packaging Information 分别写成
    # ``(TSSOP-14) - PW`` 与 ``TSSOP / PW / 14``。冻结槽位前只合并物理
    # 元数据完全相同且身份不冲突的重复证据，不能合并不同器件身份。
    before_deduplication = len(entries)
    entries = deduplicate_redundant_catalog_entries(entries)
    if len(entries) != before_deduplication:
        diagnostics.append(
            {
                "stage": "package_catalog_deduplication",
                "before": before_deduplication,
                "after": len(entries),
            }
        )

    # 第二次模型只给出封装目录，不负责判断这些封装是位于同一张表的不同
    # 字段，还是分别位于多张表。这里先完成文档结构分流，再冻结 slot。
    multi_pkg_tab_resolution = resolve_multi_pkg_tab_structure(
        target_tables=target_tables,
        catalog_entries=entries,
        multi_package_plans=multi_package_plans,
    )
    diagnostics.extend(multi_pkg_tab_resolution.diagnostics)
    entries = reconcile_multi_pkg_tab_catalog(
        entries,
        all_tables=all_tables,
        resolution=multi_pkg_tab_resolution,
        diagnostics=diagnostics,
    )

    # 总述表没有建立槽位时，严格确认的表内多封装结构可以提供槽位数量。
    # 如果仍无多封装证据，但存在目标引脚表，则整篇文档只建立一个槽位；
    # 绝不能按目标表数量建立槽位。
    entries = merge_plan_package_labels(
        entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
        diagnostics=diagnostics,
    )
    entries = add_target_figure_variant_catalog_entries(
        entries,
        target_tables=target_tables,
        diagnostics=diagnostics,
    )
    entries = add_target_figure_physical_catalog_entries(
        entries,
        target_tables=target_tables,
        diagnostics=diagnostics,
    )
    entries = consolidate_target_physical_catalog_entries(
        entries,
        target_tables=target_tables,
        diagnostics=diagnostics,
    )
    if not entries and target_tables:
        if target_tables_have_multiple_package_contexts(target_tables):
            diagnostics.append(
                {
                    "stage": "package_catalog_anonymous_slot_guard",
                    "status": "blocked",
                    "reason": "multi_target_context_without_catalog",
                }
            )
        else:
            entries = [
                PackageCatalogEntry(
                    package_key="",
                    evidence_table_ids=[target_tables[0].table_id],
                )
            ]

    # 到这里封装数量已经确定。后续绑定只能选择这些 slot，不能增删。
    freeze_package_slots(entries)

    binding_diagnostics: list[dict[str, Any]] = []
    assignments = bind_target_tables(
        entries=entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
        multi_pkg_tab_resolution=multi_pkg_tab_resolution,
        diagnostics=binding_diagnostics,
    )
    diagnostics.extend(binding_diagnostics)
    if should_apply_all_unresolved_single_package_fallback(
        entries=entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
        assignments=assignments,
        binding_diagnostics=binding_diagnostics,
    ):
        fallback_before_entries = list(entries)
        entries = [
            select_single_package_fallback_entry(
                entries,
                target_tables=target_tables,
            )
        ]
        diagnostics.append(
            single_package_all_unresolved_fallback_diagnostic(
                before_entries=fallback_before_entries,
                selected_entry=entries[0],
                target_tables=target_tables,
                initial_binding_diagnostics=binding_diagnostics,
            )
        )
        freeze_package_slots(entries)
        assignments = bind_target_tables(
            entries=entries,
            target_tables=target_tables,
            multi_package_plans=multi_package_plans,
            multi_pkg_tab_resolution=None,
            diagnostics=diagnostics,
        )
    return PackageCatalogResolution(entries, assignments, diagnostics)


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
                    source_name="",
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
                    "",
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
    if not entries:
        entries = infer_standard_catalog_entries_from_tables(
            tables,
            diagnostics=diagnostics,
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
    decision = PackageCatalogDecision(
        is_package_summary=(
            bool(response.get("is_package_summary")) and structurally_valid
        ),
        table_role=table_role if structurally_valid else "irrelevant",
        header_row_index=header_row_index,
        columns=tuple(columns),
    )
    if decision.is_package_summary:
        decision = repair_catalog_header_row_index(table, decision)
    return decision


def repair_catalog_header_row_index(
    table: PackageCatalogTable,
    decision: PackageCatalogDecision,
) -> PackageCatalogDecision:
    """纠正模型把第一条数据行误判为 catalog 表头的情况。"""

    if not table.rows or not decision.columns:
        return decision
    best_index = best_catalog_header_row_index(table)
    if best_index == decision.header_row_index:
        return decision
    current_score = catalog_header_row_score(table.rows[decision.header_row_index])
    best_score = catalog_header_row_score(table.rows[best_index])
    if best_score < 3 or best_score <= current_score:
        return decision
    return PackageCatalogDecision(
        is_package_summary=decision.is_package_summary,
        table_role=decision.table_role,
        header_row_index=best_index,
        columns=with_catalog_column_headers(table, decision.columns, best_index),
    )


def with_catalog_column_headers(
    table: PackageCatalogTable,
    columns: Sequence[PackageColumnRole],
    header_row_index: int,
) -> tuple[PackageColumnRole, ...]:
    """按修正后的表头行重建列角色对象中的 header 文本。"""

    header_row = table.rows[header_row_index] if table.rows else ()
    return tuple(
        PackageColumnRole(
            column_index=column.column_index,
            role=column.role,
            header=(
                str(header_row[column.column_index])
                if column.column_index < len(header_row)
                else table.headers[column.column_index]
                if column.column_index < len(table.headers)
                else f"column_{column.column_index + 1}"
            ),
        )
        for column in columns
    )


def best_catalog_header_row_index(table: PackageCatalogTable) -> int:
    """从表格前几行确定最像封装/器件 catalog 表头的行。"""

    candidates = list(enumerate(table.rows[: min(5, len(table.rows))]))
    if not candidates:
        return 0
    return max(
        candidates,
        key=lambda item: (catalog_header_row_score(item[1]), -item[0]),
    )[0]


def catalog_header_row_score(row: Sequence[str]) -> int:
    """给一行 catalog 表头打分；数据行通常得分为 0。"""

    roles: set[str] = set()
    for cell in row:
        roles.update(catalog_header_roles_for_cell(cell))
    if not roles:
        return 0
    score = len(roles)
    if {"package_identity", "orderable_sku"} & roles:
        score += 1
    if {"package_type", "package_drawing"} & roles:
        score += 1
    if "pin_count" in roles:
        score += 1
    return score


def catalog_header_roles_for_cell(value: str) -> set[str]:
    """识别标准 TI catalog 表头单元格可能对应的结构角色。"""

    normalized = normalize_text(value)
    compact = normalize_compact(value)
    roles: set[str] = set()
    if not normalized:
        return roles
    if (
        "orderable device" in normalized
        or "orderable part" in normalized
        or "ordering code" in normalized
        or normalized in {"sku", "orderable"}
    ):
        roles.add("orderable_sku")
    if (
        normalized in {"device", "part number", "part no", "part"}
        or "器件型号" in normalized
        or "device number" in normalized
    ):
        roles.add("package_identity")
    if (
        "package drawing" in normalized
        or "drawing" == normalized
        or "package name" in normalized
        or "封装代号" in normalized
    ):
        roles.add("package_drawing")
    if (
        "package type" in normalized
        or normalized in {"package", "packages"}
        or compact == "封装"
    ):
        roles.add("package_type")
    if (
        normalized in {"pins", "pin count", "pin counts", "terminals"}
        or normalized.startswith("pins ")
        or "引脚数" in normalized
    ):
        roles.add("pin_count")
    return roles


def infer_standard_catalog_entries_from_tables(
    tables: Sequence[PackageCatalogTable],
    *,
    diagnostics: list[dict[str, Any]],
    target_tables: Sequence[PackageTargetTable],
) -> list[PackageCatalogEntry]:
    """模型未建立 catalog 时，用 TI 标准表头确定性兜底。"""

    priority_tables = [
        table for table in tables if _has_priority_catalog_title(table.title)
    ]
    for selected_tables, scope in (
        (priority_tables, "priority_title"),
        (list(tables), "all_candidates"),
    ):
        decisions = standard_catalog_decisions_from_tables(selected_tables)
        if not decisions:
            continue
        local_diagnostics: list[dict[str, Any]] = []
        entries = build_catalog_entries_from_decisions(
            decisions,
            local_diagnostics,
            target_tables=target_tables,
        )
        if not entries:
            continue
        diagnostics.append(
            {
                "stage": "package_catalog_standard_fallback",
                "status": "applied",
                "scope": scope,
                "table_ids": [table.table_id for _, table, _ in decisions],
                "reason": "empty_model_catalog",
            }
        )
        diagnostics.extend(local_diagnostics)
        return entries
    return []


def standard_catalog_decisions_from_tables(
    tables: Sequence[PackageCatalogTable],
) -> list[tuple[int, PackageCatalogTable, PackageCatalogDecision]]:
    """从强标准表头中构造等价于第二次模型返回的结构判断。"""

    decisions: list[tuple[int, PackageCatalogTable, PackageCatalogDecision]] = []
    for table in tables:
        decision = standard_catalog_decision_from_table(table)
        if decision is None:
            continue
        decisions.append((table.table_id, table, decision))
    return decisions


def standard_catalog_decision_from_table(
    table: PackageCatalogTable,
) -> PackageCatalogDecision | None:
    """识别常见 TI 器件/封装/订购信息表，不依赖模型输出。"""

    if not table.rows:
        return None
    header_row_index = best_catalog_header_row_index(table)
    header_row = table.rows[header_row_index]
    column_role_hints = [
        catalog_header_roles_for_cell(cell)
        for cell in header_row
    ]
    has_orderable = any("orderable_sku" in roles for roles in column_role_hints)
    has_identity = any("package_identity" in roles for roles in column_role_hints)
    has_package_type = any("package_type" in roles for roles in column_role_hints)
    has_package_drawing = any(
        "package_drawing" in roles for roles in column_role_hints
    )
    has_pin_count = any("pin_count" in roles for roles in column_role_hints)

    if has_orderable or (
        has_identity and has_package_type and has_package_drawing and has_pin_count
    ):
        table_role = "packaging_metadata"
    elif has_identity and has_package_type:
        table_role = "identity_summary"
    else:
        return None

    columns: list[PackageColumnRole] = []
    for column_index, roles in enumerate(column_role_hints):
        role = standard_catalog_role_for_column(
            roles,
            table_role=table_role,
        )
        if not role:
            continue
        columns.append(
            PackageColumnRole(
                column_index=column_index,
                role=role,
                header=str(header_row[column_index]),
            )
        )
    role_names = {column.role for column in columns}
    structurally_valid = (
        table_role == "identity_summary"
        and "package_identity" in role_names
        and "package_type" in role_names
    ) or (
        table_role == "packaging_metadata"
        and "orderable_sku" in role_names
        and (
            "package_type" in role_names
            or "package_drawing" in role_names
        )
    )
    if not structurally_valid:
        return None
    return PackageCatalogDecision(
        is_package_summary=True,
        table_role=table_role,
        header_row_index=header_row_index,
        columns=tuple(columns),
    )


def standard_catalog_role_for_column(
    roles: set[str],
    *,
    table_role: str,
) -> str:
    """根据整表角色把泛化的 Device/Package 表头落到具体列角色。"""

    if "orderable_sku" in roles:
        return "orderable_sku"
    if "package_identity" in roles:
        return (
            "orderable_sku"
            if table_role == "packaging_metadata"
            else "package_identity"
        )
    for role in ("package_drawing", "package_type", "pin_count"):
        if role in roles:
            return role
    return ""


def build_catalog_entries_from_decisions(
    decisions: Sequence[
        tuple[int, PackageCatalogTable, PackageCatalogDecision]
    ],
    diagnostics: list[dict[str, Any]],
    *,
    target_tables: Sequence[PackageTargetTable] = (),
) -> list[PackageCatalogEntry]:
    """先建立身份槽位，再补元数据；必要时由包装结构建立物理槽位。"""

    entries: list[PackageCatalogEntry] = []

    # 第一遍只处理身份总述表。无论包装信息表在文档中位于前部还是后部，
    # 都必须等器件身份槽位建立后才能参与补充。
    for _, table, decision in decisions:
        if decision.table_role != "identity_summary":
            continue
        created_names = create_identity_entries_from_table(
            entries,
            table,
            decision,
        )
        diagnostics.append(
            {
                "stage": "package_catalog_rows",
                "table_id": table.table_id,
                "table_role": decision.table_role,
                "created_or_merged": created_names,
            }
        )

    # 第二遍优先给已有身份槽位补元数据。
    for _, table, decision in decisions:
        if decision.table_role != "packaging_metadata":
            continue
        enriched_names = enrich_entries_from_packaging_table(
            entries,
            table,
            decision,
        )
        diagnostics.append(
            {
                "stage": "package_catalog_rows",
                "table_id": table.table_id,
                "table_role": decision.table_role,
                "enriched": enriched_names,
            }
        )

    # 没有独立身份总述表时，订购表中的 SKU 仍可与目标引脚表题/章节中已经
    # 出现的器件身份做严格前缀关联。例如 SF2507BC 与 SF2507、SF2507EBC
    # 与 SF2507E。代码只接受目标表真实出现过的最长身份前缀，不自行截 SKU。
    for _, table, decision in decisions:
        if decision.table_role != "packaging_metadata":
            continue
        derived = create_target_identity_entries_from_packaging_table(
            table,
            decision,
            target_tables,
        )
        for entry in derived:
            merge_catalog_entry(entries, entry)
        if derived:
            diagnostics.append(
                {
                    "stage": "package_catalog_rows",
                    "table_id": table.table_id,
                    "table_role": decision.table_role,
                    "created_from_target_identity": [
                        {
                            "identity_name": entry.identity_name,
                            "package_type": entry.package_type,
                            "package_drawing": entry.package_drawing,
                            "pin_count": entry.pin_count,
                        }
                        for entry in derived
                    ],
                }
            )

    # 某些文档没有单独的器件身份总述表，只有 Packaging Information。
    # 此时 package_type + drawing + pin_count 的唯一组合足以确定物理槽位，
    # 但 orderable_sku 本身仍不能成为公开 pkg。
    if not entries:
        for _, table, decision in decisions:
            if decision.table_role != "packaging_metadata":
                continue
            created = create_entries_from_packaging_table(table, decision)
            for entry in created:
                merge_physical_metadata_entry(entries, entry)
            if created:
                diagnostics.append(
                    {
                        "stage": "package_catalog_rows",
                        "table_id": table.table_id,
                        "table_role": decision.table_role,
                        "created_physical_slots": [
                            {
                                "package_type": entry.package_type,
                                "package_drawing": entry.package_drawing,
                                "pin_count": entry.pin_count,
                            }
                            for entry in created
                        ],
                    }
                )
    return entries


def create_target_identity_entries_from_packaging_table(
    table: PackageCatalogTable,
    decision: PackageCatalogDecision,
    target_tables: Sequence[PackageTargetTable],
) -> list[PackageCatalogEntry]:
    """用订购 SKU 与目标表中的器件身份建立独立封装槽位。

    该函数不从 SKU 猜测型号。只有一个字母数字标识符已经出现在目标引脚表
    的表题或章节标题中，并且它是 SKU 的完整前缀时才允许关联；多个候选
    同时匹配时取最长者，避免 ``SF2507E`` 被较短的 ``SF2507`` 抢占。
    """

    identity_candidates = collect_target_identity_candidates(target_tables)
    if not identity_candidates:
        return []

    sku_columns = columns_for_role(decision, "orderable_sku")
    matched_rows: list[tuple[str, Sequence[str]]] = []
    for row in table.rows[decision.header_row_index + 1 :]:
        sku_values = [
            cell_value(row, column)
            for column in sku_columns
            if cell_value(row, column)
        ]
        identity = longest_target_identity_prefix(
            sku_values,
            identity_candidates,
        )
        if identity:
            matched_rows.append((identity, row))

    # 只有一项身份时无法证明多个订购行是否代表独立 pinout。此时保留原有
    # 物理元数据/表内分支逻辑，避免把同一器件的温度或包装后缀误拆成 pkg。
    distinct_identities = {
        normalize_compact(identity)
        for identity, _ in matched_rows
    }
    if len(distinct_identities) < 2:
        return []

    result: list[PackageCatalogEntry] = []
    for identity, row in matched_rows:
        package_type = first_role_value(row, decision, "package_type")
        pin_count = clean_pin_count(
            first_role_value(row, decision, "pin_count")
        )
        package_type, type_pin_count = split_package_type_and_pin_count(
            package_type
        )
        incoming = PackageCatalogEntry(
            package_key="",
            identity_name=identity,
            package_type=clean_public_package_name(package_type),
            package_drawing=first_role_value(
                row,
                decision,
                "package_drawing",
            ),
            pin_count=pin_count or type_pin_count,
            evidence_table_ids=[table.table_id],
        )
        merge_catalog_entry(result, incoming)
    return result


def collect_target_identity_candidates(
    target_tables: Sequence[PackageTargetTable],
) -> dict[str, str]:
    """收集目标表局部标题中真实出现的短器件标识符。"""

    candidates: dict[str, str] = {}
    for table in target_tables:
        texts = [table.title, *table.current_chapter_titles]
        for text in texts:
            for token in re.findall(
                r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9-]{3,14})(?![A-Za-z0-9])",
                str(text or ""),
            ):
                compact = normalize_compact(token)
                # 器件标识符必须同时含字母和数字。Table、Package、LQFP 等
                # 普通标题词不满足该条件，也不会进入 SKU 前缀匹配。
                if (
                    not re.search(r"[a-z]", compact)
                    or not re.search(r"\d", compact)
                ):
                    continue
                cleaned = clean_identity_name(token)
                if cleaned:
                    candidates.setdefault(compact, cleaned)
    return candidates


def longest_target_identity_prefix(
    sku_values: Sequence[str],
    identity_candidates: Mapping[str, str],
) -> str:
    """返回与任一订购 SKU 匹配的最长目标表身份前缀。"""

    matches: list[tuple[int, str]] = []
    for sku_value in sku_values:
        sku = normalize_compact(sku_value)
        for compact, display_name in identity_candidates.items():
            if sku.startswith(compact):
                matches.append((len(compact), display_name))
    if not matches:
        return ""
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0][1]


def create_identity_entries_from_table(
    entries: list[PackageCatalogEntry],
    table: PackageCatalogTable,
    decision: PackageCatalogDecision,
) -> list[str]:
    """从身份总述表逐行创建槽位；器件型号只保存为内部关联键。"""

    created_names: list[str] = []
    identity_columns = columns_for_role(decision, "package_identity")
    for row in table.rows[decision.header_row_index + 1 :]:
        for identity_column in identity_columns:
            identity_name = clean_identity_name(
                cell_value(row, identity_column)
            )
            if (
                not identity_name
                or is_generic_package_label(identity_name)
                or is_repeated_header_value(
                    identity_name,
                    identity_column.header,
                )
            ):
                continue
            package_type = first_role_value(row, decision, "package_type")
            pin_count = clean_pin_count(
                first_role_value(row, decision, "pin_count")
            )
            package_type, type_pin_count = split_package_type_and_pin_count(
                package_type
            )
            package_type = clean_public_package_name(package_type)
            incoming = PackageCatalogEntry(
                package_key="",
                identity_name=identity_name,
                package_type=package_type,
                package_drawing=first_role_value(
                    row,
                    decision,
                    "package_drawing",
                ),
                pin_count=pin_count or type_pin_count,
                evidence_table_ids=[table.table_id],
            )
            merge_catalog_entry(entries, incoming)
            if identity_name not in created_names:
                created_names.append(identity_name)
    return created_names


def create_entries_from_packaging_table(
    table: PackageCatalogTable,
    decision: PackageCatalogDecision,
) -> list[PackageCatalogEntry]:
    """从包装表读取物理封装组合，不把订购型号误写为 pkg。"""

    result: list[PackageCatalogEntry] = []
    for row in table.rows[decision.header_row_index + 1 :]:
        package_type = first_role_value(row, decision, "package_type")
        package_type, type_pin_count = split_package_type_and_pin_count(
            package_type
        )
        package_type = clean_public_package_name(package_type)
        package_drawing = clean_metadata(
            first_role_value(row, decision, "package_drawing")
        )
        pin_count = clean_pin_count(
            first_role_value(row, decision, "pin_count")
        ) or type_pin_count
        if not package_type:
            continue
        result.append(
            PackageCatalogEntry(
                package_key="",
                package_type=package_type,
                package_drawing=package_drawing,
                pin_count=pin_count,
                evidence_table_ids=[table.table_id],
            )
        )
    return result


def enrich_entries_from_packaging_table(
    entries: list[PackageCatalogEntry],
    table: PackageCatalogTable,
    decision: PackageCatalogDecision,
) -> list[str]:
    """按订购型号前缀关联已有 pkg，并补充包装元数据。"""

    enriched_names: list[str] = []
    lookup_columns = [
        *columns_for_role(decision, "package_identity"),
        *columns_for_role(decision, "orderable_sku"),
    ]
    for row in table.rows[decision.header_row_index + 1 :]:
        lookup_values = [
            cell_value(row, column)
            for column in lookup_columns
            if cell_value(row, column)
        ]
        entry = match_existing_entry_from_orderable_values(entries, lookup_values)
        if entry is None:
            continue

        package_type = first_role_value(row, decision, "package_type")
        pin_count = clean_pin_count(
            first_role_value(row, decision, "pin_count")
        )
        package_type, type_pin_count = split_package_type_and_pin_count(
            package_type
        )
        update_entry_metadata(
            entry,
            package_type=package_type,
            package_drawing=first_role_value(
                row,
                decision,
                "package_drawing",
            ),
            pin_count=pin_count or type_pin_count,
            evidence_table_id=table.table_id,
        )
        if entry.identity_name not in enriched_names:
            enriched_names.append(entry.identity_name)
    return enriched_names


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


def match_existing_entry_from_orderable_values(
    entries: Sequence[PackageCatalogEntry],
    values: Sequence[str],
) -> PackageCatalogEntry | None:
    """用完整身份或订购型号前缀匹配已有 pkg，歧义时不补充。"""

    ranked: list[tuple[int, PackageCatalogEntry]] = []
    for entry in entries:
        identity = normalize_compact(entry.identity_name)
        if not identity:
            continue
        for value in values:
            candidate = normalize_compact(value)
            if candidate == identity or candidate.startswith(identity):
                ranked.append((len(identity), entry))
                break
    if not ranked:
        return None
    best_length = max(length for length, _ in ranked)
    best_entries = [
        entry
        for length, entry in ranked
        if length == best_length
    ]
    # 槽位此时尚未执行 freeze_package_slots，package_key 仍为空，不能拿它
    # 判断歧义；器件身份才是这一阶段可用的唯一稳定键。
    unique = {
        normalize_compact(entry.identity_name): entry
        for entry in best_entries
    }
    return next(iter(unique.values())) if len(unique) == 1 else None


def update_entry_metadata(
    entry: PackageCatalogEntry,
    *,
    package_type: str,
    package_drawing: str,
    pin_count: str,
    evidence_table_id: int,
) -> None:
    """只填充尚未确定的元数据，不能覆盖身份总述表已有信息。"""

    if package_type and not entry.package_type:
        entry.package_type = package_type
    if package_drawing and not entry.package_drawing:
        entry.package_drawing = package_drawing
    if pin_count and not entry.pin_count:
        entry.pin_count = pin_count
    if evidence_table_id not in entry.evidence_table_ids:
        entry.evidence_table_ids.append(evidence_table_id)


def merge_catalog_entry(
    entries: list[PackageCatalogEntry],
    incoming: PackageCatalogEntry,
) -> None:
    """只按器件身份或明确别名合并，不按封装类型猜测。"""

    incoming_names = {
        normalize_compact(incoming.identity_name),
        *(normalize_compact(alias) for alias in incoming.identity_aliases),
    }
    for existing in entries:
        existing_names = {
            normalize_compact(existing.identity_name),
            *(normalize_compact(alias) for alias in existing.identity_aliases),
        }
        if incoming_names.isdisjoint(existing_names):
            continue
        for alias in [
            incoming.identity_name,
            *incoming.identity_aliases,
        ]:
            if (
                alias != existing.identity_name
                and alias not in existing.identity_aliases
            ):
                existing.identity_aliases.append(alias)
        for table_id in incoming.evidence_table_ids:
            if table_id not in existing.evidence_table_ids:
                existing.evidence_table_ids.append(table_id)
        if not existing.package_type:
            existing.package_type = incoming.package_type
        if not existing.package_drawing:
            existing.package_drawing = incoming.package_drawing
        if not existing.pin_count:
            existing.pin_count = incoming.pin_count
        if not existing.public_label:
            existing.public_label = incoming.public_label
        return
    entries.append(incoming)


def merge_physical_metadata_entry(
    entries: list[PackageCatalogEntry],
    incoming: PackageCatalogEntry,
) -> None:
    """包装表无身份列时，按完整物理元数据组合去重槽位。

    package_type 相同但 drawing 或 pin_count 不同，仍保留为不同槽位。
    """

    incoming_key = physical_metadata_key(incoming)
    for existing in entries:
        if physical_metadata_key(existing) != incoming_key:
            continue
        for table_id in incoming.evidence_table_ids:
            if table_id not in existing.evidence_table_ids:
                existing.evidence_table_ids.append(table_id)
        if not existing.public_label:
            existing.public_label = incoming.public_label
        return
    entries.append(incoming)


def physical_metadata_key(entry: PackageCatalogEntry) -> tuple[str, str, str]:
    """生成包装表槽位去重键，避免仅凭一个 QFN 字符串错误归并。"""

    return (
        normalize_compact(entry.package_type),
        normalize_compact(entry.package_drawing),
        clean_pin_count(entry.pin_count),
    )


def deduplicate_redundant_catalog_entries(
    entries: Sequence[PackageCatalogEntry],
) -> list[PackageCatalogEntry]:
    """合并同一物理封装的重复表述，同时保留不同身份的独立槽位。

    只有 family、drawing、pin_count 三项都能确定且完全一致时才允许合并；
    两个非空器件身份不同则强制保留，避免把同封装类型的不同映射空间合并。
    """

    result: list[PackageCatalogEntry] = []
    for incoming in entries:
        incoming_signature = canonical_physical_metadata(incoming)
        incoming_identity = normalize_compact(incoming.identity_name)
        duplicate: PackageCatalogEntry | None = None

        for existing in result:
            existing_identity = normalize_compact(existing.identity_name)
            identities_conflict = bool(
                incoming_identity
                and existing_identity
                and incoming_identity != existing_identity
            )
            if identities_conflict:
                continue
            if (
                incoming_signature[0]
                and incoming_signature[1]
                and incoming_signature[2]
                and incoming_signature == canonical_physical_metadata(existing)
            ):
                duplicate = existing
                break

        if duplicate is None:
            result.append(incoming)
            continue
        merge_redundant_catalog_evidence(duplicate, incoming)
    return result


def canonical_physical_metadata(
    entry: PackageCatalogEntry,
) -> tuple[str, str, str]:
    """把组合写法转换为仅用于比较的 family/drawing/pin_count。"""

    package_type = clean_metadata(entry.package_type)
    drawing = clean_metadata(entry.package_drawing)
    pin_count = clean_pin_count(entry.pin_count)

    # 数据手册常用 ``(TSSOP-14) - PW`` 表示封装族、引脚数和 drawing。
    # 该格式三项证据齐全，可以与分列元数据安全比较；SC-70 这类普通
    # 封装名不会命中该规则，因此不会把 70 误当作引脚数。
    combined_match = re.fullmatch(
        r"\(\s*([A-Za-z][A-Za-z0-9]*)\s*-\s*(\d+)\s*\)"
        r"\s*[-–—]\s*([A-Za-z0-9]+)",
        package_type,
    )
    if combined_match:
        package_type = combined_match.group(1)
        pin_count = pin_count or combined_match.group(2)
        drawing = drawing or combined_match.group(3)

    # 只有显式包含 PIN 的写法才从普通文本中提取数量，避免把 SC-70、
    # QFN-16 等名称内部数字在证据不足时直接解释成 pin_count。
    explicit_pin_match = re.fullmatch(
        r"(.+?)\s+(\d+)\s*[- ]?\s*pin(?:s)?",
        package_type,
        flags=re.IGNORECASE,
    )
    if explicit_pin_match:
        package_type = clean_metadata(explicit_pin_match.group(1))
        pin_count = pin_count or explicit_pin_match.group(2)

    return (
        normalize_compact(package_type),
        normalize_compact(drawing),
        clean_pin_count(pin_count),
    )


def merge_redundant_catalog_evidence(
    target: PackageCatalogEntry,
    incoming: PackageCatalogEntry,
) -> None:
    """把重复目录项的身份、别名和更结构化的物理元数据合入原槽位。"""

    if not target.identity_name and incoming.identity_name:
        target.identity_name = incoming.identity_name
    for alias in [incoming.identity_name, *incoming.identity_aliases]:
        if alias and alias != target.identity_name and alias not in target.identity_aliases:
            target.identity_aliases.append(alias)
    for table_id in incoming.evidence_table_ids:
        if table_id not in target.evidence_table_ids:
            target.evidence_table_ids.append(table_id)
    if not target.public_label and incoming.public_label:
        target.public_label = incoming.public_label

    # 分列字段比组合字符串更适合作为公开名称和后续匹配证据。
    target_detail = bool(target.package_drawing) + bool(target.pin_count)
    incoming_detail = bool(incoming.package_drawing) + bool(incoming.pin_count)
    if incoming_detail > target_detail:
        target.package_type = incoming.package_type or target.package_type
        target.package_drawing = incoming.package_drawing or target.package_drawing
        target.pin_count = incoming.pin_count or target.pin_count
    else:
        if not target.package_type:
            target.package_type = incoming.package_type
        if not target.package_drawing:
            target.package_drawing = incoming.package_drawing
        if not target.pin_count:
            target.pin_count = incoming.pin_count


def reconcile_multi_pkg_tab_catalog(
    entries: Sequence[PackageCatalogEntry],
    *,
    all_tables: Sequence[PackageCatalogTable],
    resolution: MultiPkgTabResolution,
    diagnostics: list[dict[str, Any]],
) -> list[PackageCatalogEntry]:
    """用跨表引脚表证据整理第二次模型得到的封装目录。

    本函数只在槽位冻结前工作：同一 Drawing/pin_count 被多张目标表明确证明
    为同一分支时，允许合并不同订购型号产生的重复目录项；目标表明确出现
    一个目录中缺失的分支时，补建匿名槽位并把局部标签保存为内部别名。目标
    表已经确认的短分支标签会额外写入 ``public_label``，供最终 ``pkg`` 使用。
    """

    result = list(entries)
    index_to_entry = {
        index: entry
        for index, entry in enumerate(entries)
    }

    # 只有目标表按 Drawing 明确归为同一分支时才跨身份合并。原有目录去重
    # 仍保持严格，避免仅凭相同 QFN/BGA 封装族误合并不同引脚映射。
    for index_group in catalog_groups_confirmed_by_target_tables(resolution):
        available = [
            index_to_entry[index]
            for index in index_group
            if index in index_to_entry
        ]
        unique_available = list({id(entry): entry for entry in available}.values())
        if len(unique_available) < 2:
            continue
        target = unique_available[0]
        for incoming in unique_available[1:]:
            merge_redundant_catalog_evidence(target, incoming)
            if incoming in result:
                result.remove(incoming)
        for index in index_group:
            index_to_entry[index] = target
        diagnostics.append(
            {
                "stage": "multi_pkg_tab_catalog_merge",
                "catalog_entry_indexes": list(index_group),
                "identity_name": target.identity_name,
                "identity_aliases": list(target.identity_aliases),
                "package_drawing": target.package_drawing,
                "pin_count": target.pin_count,
                "public_label": target.public_label,
            }
        )

    supported_entry_ids: set[int] = set()
    for branch in resolution.branches:
        matched_entries = list(
            {
                id(index_to_entry[index]): index_to_entry[index]
                for index in branch.catalog_entry_indexes
                if index in index_to_entry
            }.values()
        )

        # 目录索引不足时，再用精确分支标签匹配整理后的目录。只接受唯一项，
        # 多项同名仍保持歧义，不能选择第一个。
        if not matched_entries:
            matched_entries = _entries_matching_exact_branch_label(
                result,
                branch.label,
            )

        if len(matched_entries) == 1:
            entry = matched_entries[0]
            if _branch_label_can_be_public(branch.evidence_kind):
                _record_confirmed_branch_label(entry, branch.label)
            else:
                _append_internal_binding_alias(entry, branch.label)
            supported_entry_ids.add(id(entry))
            diagnostics.append(
                {
                    "stage": "multi_pkg_tab_catalog_support",
                    "branch_key": branch.branch_key,
                    "branch_label": branch.label,
                    "table_ids": list(branch.table_ids),
                    "status": "matched_catalog_entry",
                }
            )
            continue

        # 只有已经形成至少两个跨表分支时，局部标签才能证明目录确实漏掉了
        # 一个槽位。单个普通标题不能单独扩大文档 pkg 数量。
        if (
            not matched_entries
            and resolution.document_mode
            in {"cross_table_multi_package", "mixed_multi_package"}
        ):
            entry = PackageCatalogEntry(
                package_key="",
                identity_aliases=[branch.label],
                evidence_table_ids=list(branch.table_ids),
                public_label=(
                    clean_public_symbol_name(branch.label)
                    if _branch_label_can_be_public(branch.evidence_kind)
                    else ""
                ),
            )
            result.append(entry)
            supported_entry_ids.add(id(entry))
            diagnostics.append(
                {
                    "stage": "multi_pkg_tab_catalog_support",
                    "branch_key": branch.branch_key,
                    "branch_label": branch.label,
                    "table_ids": list(branch.table_ids),
                    "status": "created_missing_slot",
                }
            )

    # 已经形成明确跨表分支时，区域召回得到的弱总述表不能再额外增加 pkg。
    # 但“Device/Package/Ordering Information”等自身表题的高优先级证据仍
    # 保留，因为它可能描述尚未被第一次模型选中的真实封装。
    if (
        len(resolution.branches) >= 2
        and supported_entry_ids
    ):
        tables_by_id = {table.table_id: table for table in all_tables}
        filtered: list[PackageCatalogEntry] = []
        removed: list[dict[str, Any]] = []
        for entry in result:
            if id(entry) in supported_entry_ids or _entry_has_priority_catalog_evidence(
                entry,
                tables_by_id,
            ):
                filtered.append(entry)
                continue
            removed.append(
                {
                    "identity_name": entry.identity_name,
                    "package_type": entry.package_type,
                    "package_drawing": entry.package_drawing,
                    "pin_count": entry.pin_count,
                    "evidence_table_ids": list(entry.evidence_table_ids),
                    "public_label": entry.public_label,
                }
            )
        if removed:
            diagnostics.append(
                {
                    "stage": "multi_pkg_tab_weak_catalog_filter",
                    "removed": removed,
                }
            )
        result = filtered

    return result


def _entries_matching_exact_branch_label(
    entries: Sequence[PackageCatalogEntry],
    label: str,
) -> list[PackageCatalogEntry]:
    """按内部身份、别名或 Drawing 精确匹配一个跨表分支。"""

    normalized_label = normalize_compact(label)
    matches = []
    for entry in entries:
        values = [
            entry.identity_name,
            *entry.identity_aliases,
            entry.package_drawing,
            entry.public_label,
        ]
        if any(normalize_compact(value) == normalized_label for value in values):
            matches.append(entry)
    return matches


def _record_confirmed_branch_label(
    entry: PackageCatalogEntry,
    label: str,
) -> None:
    """保存已确认分支标签，同时把安全短标签暴露给最终 pkg。"""

    _append_internal_binding_alias(entry, label)
    public_label = clean_public_symbol_name(label)
    if public_label and not entry.public_label:
        entry.public_label = public_label


def _branch_label_can_be_public(evidence_kind: str) -> bool:
    """只有明确封装分支/drawing 证据可以成为公开短标签。"""

    return evidence_kind in {"package_drawing", "explicit_package_label"}


def _append_internal_binding_alias(
    entry: PackageCatalogEntry,
    label: str,
) -> None:
    """保存局部分支标签供后续表绑定使用。"""

    label = str(label or "").strip()
    if (
        label
        and label != entry.identity_name
        and label not in entry.identity_aliases
    ):
        entry.identity_aliases.append(label)


def _entry_has_priority_catalog_evidence(
    entry: PackageCatalogEntry,
    tables_by_id: Mapping[int, PackageCatalogTable],
) -> bool:
    """判断目录项是否来自自身表题明确的高优先级封装总述表。"""

    return any(
        table is not None and _has_priority_catalog_title(table.title)
        for table_id in entry.evidence_table_ids
        for table in [tables_by_id.get(table_id)]
    )


def merge_plan_package_labels(
    entries: list[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[PackageCatalogEntry]:
    """用最完整的多封装计划保证文档目录具有足够的独立槽位。

    不能累计每张表的标签，否则同一组封装在多个表中重复出现时会被重复计数。
    如果总述阶段只识别到一个公开封装名称，而表内结构严格确认存在 N 个
    分支，则复制该封装元数据建立 N 个独立槽位。复制的是名称和物理元数据，
    不是数据桶；每个分支随后仍按自己的 ``package_key`` 独立提取。
    """

    result = list(entries)
    target_ids = {table.table_id for table in target_tables}
    eligible_plans = [
        (table_id, plan)
        for table_id, plan in multi_package_plans.items()
        if (
            table_id in target_ids
            and plan_creates_package_slots(plan)
            and len(plan.bindings) >= 2
        )
    ]
    if not eligible_plans:
        return result

    # 槽位最多的计划通常是完整封装映射表；同数量时保持文档顺序。
    table_id, anchor_plan = max(
        eligible_plans,
        key=lambda item: (len(item[1].bindings), -item[0]),
    )

    required_slots = len(anchor_plan.bindings)

    # 第二次模型可能从 Device/Ordering Information 中返回多条器件型号记录。
    # 器件型号是绑定证据，不等于物理封装槽位。只有 package_columns 已经用
    # N 个独立 pin_no 列严格证明存在 N 个物理映射空间时，才把目录中超过 N
    # 的“型号行”降回证据。名称分支、行分段和局部续表不参与这次收紧，避免
    # 一张只覆盖部分封装的局部表裁剪全文目录。
    if anchor_plan.mode == "package_columns" and len(result) > required_slots:
        reconciled, reconciliation = reconcile_catalog_to_confirmed_plan(
            result,
            required_slots=required_slots,
            evidence_table_id=table_id,
        )
        if reconciliation is not None:
            result = reconciled
            if diagnostics is not None:
                diagnostics.append(reconciliation)

    # 一个真实封装名称不能覆盖已经确认的多个表内分支。为每个分支复制一个
    # 独立目录项，freeze_package_slots() 随后会为它们分配不同的 slot key。
    # 公开名称允许相同，最终 JSON 的统一后缀逻辑负责生成 WQFN1/WQFN2……。
    if len(result) == 1 and required_slots > 1:
        template = result[0]
        evidence_table_ids = list(template.evidence_table_ids)
        if table_id not in evidence_table_ids:
            evidence_table_ids.append(table_id)
        return [
            replace(
                template,
                package_key="",
                identity_aliases=list(template.identity_aliases),
                evidence_table_ids=list(evidence_table_ids),
            )
            for _binding in anchor_plan.bindings
        ]

    # 总述可能只识别出部分封装。严格表头已经确认 N 个独立分支时，目录槽位
    # 至少也必须有 N 个；缺失槽位保持匿名，不能拿分支标签冒充真实封装名。
    if 1 < len(result) < required_slots:
        for _slot_index in range(len(result), required_slots):
            result.append(
                PackageCatalogEntry(
                    package_key="",
                    evidence_table_ids=[table_id],
                )
            )
        return result

    # 总述已经确认足够的槽位时，不能再根据某一张表增删目录数量。
    if result:
        return result

    for binding in anchor_plan.bindings:
        package_type = clean_public_package_name(binding.package)
        result.append(
            PackageCatalogEntry(
                package_key="",
                package_type=package_type,
                evidence_table_ids=[table_id],
            )
        )
    return result


def reconcile_catalog_to_confirmed_plan(
    entries: Sequence[PackageCatalogEntry],
    *,
    required_slots: int,
    evidence_table_id: int,
) -> tuple[list[PackageCatalogEntry], dict[str, Any] | None]:
    """用严格表内分支约束纯器件身份目录项的槽位数量。

    该函数只在“目录项数量大于严格 package_columns 分支数量”时调用，且不
    改变普通单封装、名称分支、行分段和没有多分支证据的文档。处理规则如下：

    * ``package_type/drawing/pin_count`` 均为空的项只是器件身份，不能创建槽位；
    * 有物理元数据的项保持原有独立身份，本函数不负责跨身份合并；
    * 强物理项不超过严格分支数时，以严格分支数补齐最终槽位；
    * 强物理项已经超过严格分支数时说明证据冲突，保持原目录，不武断删减。

    这样只收紧“弱目录扩大 pkg 数量”这一条边界，不会用某一张局部表覆盖
    文档中已经明确存在的更多不同物理封装。
    """

    if required_slots < 2 or len(entries) <= required_slots:
        return list(entries), None

    physical_entries: list[PackageCatalogEntry] = []
    physical_signatures: list[tuple[str, str, str]] = []
    identity_only_entries: list[PackageCatalogEntry] = []

    for entry in entries:
        signature = canonical_physical_metadata(entry)
        if not any(signature):
            identity_only_entries.append(entry)
            continue

        # 强物理目录项可能代表“相同封装名称、不同器件映射空间”。此前目录
        # 阶段已经按自己的规则完成去重，这里只复制并保留，绝不跨身份合并。
        physical_signatures.append(signature)
        physical_entries.append(
            replace(
                entry,
                package_key="",
                identity_aliases=list(entry.identity_aliases),
                evidence_table_ids=list(entry.evidence_table_ids),
            )
        )

    # 不同物理签名已经超过表内分支数量时，当前表可能只覆盖文档的一部分
    # package。此时不能根据局部表删目录，继续保留原有结果。
    if len(physical_entries) > required_slots:
        return list(entries), None

    evidence_ids = list(
        dict.fromkeys(
            [
                table_id
                for entry in entries
                for table_id in entry.evidence_table_ids
            ]
            + [evidence_table_id]
        )
    )
    result = list(physical_entries)

    if not result:
        # 目录全是器件型号时，真实 pkg 名暂时未知，但严格表头已经证明槽位
        # 数量。建立 N 个匿名槽位，最终 JSON 使用 a/b/c，而不是按型号行计数。
        result = [
            PackageCatalogEntry(
                package_key="",
                evidence_table_ids=list(evidence_ids),
            )
            for _slot_index in range(required_slots)
        ]
    elif len(result) == 1 and required_slots > 1:
        # 两个映射空间可以具有相同物理封装名称；复制槽位而不是合并数据桶。
        template = result[0]
        result = [
            replace(
                template,
                package_key="",
                identity_aliases=list(template.identity_aliases),
                evidence_table_ids=list(evidence_ids),
            )
            for _slot_index in range(required_slots)
        ]
    else:
        while len(result) < required_slots:
            result.append(
                PackageCatalogEntry(
                    package_key="",
                    evidence_table_ids=list(evidence_ids),
                )
            )

    return result, {
        "stage": "package_catalog_confirmed_plan_reconciliation",
        "status": "reconciled",
        "evidence_table_id": evidence_table_id,
        "required_slots": required_slots,
        "before": len(entries),
        "after": len(result),
        "physical_signatures": [list(value) for value in physical_signatures],
        "identity_only_entries_removed": [
            {
                "identity_name": entry.identity_name,
                "identity_aliases": list(entry.identity_aliases),
                "evidence_table_ids": list(entry.evidence_table_ids),
            }
            for entry in identity_only_entries
        ],
    }


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


def freeze_package_slots(entries: Sequence[PackageCatalogEntry]) -> None:
    """按首次出现顺序冻结槽位 key；名称变化不能改变分组身份。"""

    for slot_index, entry in enumerate(entries):
        entry.package_key = f"slot:{slot_index}"


def should_apply_all_unresolved_single_package_fallback(
    *,
    entries: Sequence[PackageCatalogEntry],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    assignments: Mapping[tuple[int, int], PackageAssignment],
    binding_diagnostics: Sequence[dict[str, Any]],
) -> bool:
    """多个目录槽位全部无法绑定时，把该文档按单封装兜底处理。

    该保底只覆盖原本会 0 输出的单封装误膨胀场景。若任一目标表已经绑定成功，
    或表内存在真实多封装结构，或失败原因是明确歧义，都不在这里合并。
    """

    if len(entries) <= 1 or not target_tables or assignments:
        return False
    if target_tables_have_multiple_package_contexts(target_tables):
        return False
    if any(
        plan_creates_package_slots(multi_package_plans.get(table.table_id))
        for table in target_tables
    ):
        return False

    package_binding_diagnostics = [
        diagnostic
        for diagnostic in binding_diagnostics
        if diagnostic.get("stage") == "package_binding"
    ]
    if not package_binding_diagnostics:
        return False
    return all(
        diagnostic.get("status") == "unresolved"
        and diagnostic.get("reason") == "package_unresolved"
        for diagnostic in package_binding_diagnostics
    )


def target_tables_have_multiple_package_contexts(
    target_tables: Sequence[PackageTargetTable],
) -> bool:
    """目标表局部文本明确出现多个封装目标时，禁止单封装兜底。

    这里只读取 PDF 正文中已经绑定到目标表附近的表题、上方图题、章节标题
    和表头；不读取文件名。证据包括完整变体型号（DRV8145H-Q1）、明确
    package+pin_count（VQFN-HR (16)）以及独立 pin_count（16-pin）。
    """

    context_keys: set[tuple[str, str]] = set()
    for table in target_tables:
        context_keys.update(package_context_keys_from_target_table(table))
        if len(context_keys) >= 2:
            return True
    return False


def package_context_keys_from_target_table(
    table: PackageTargetTable,
) -> set[tuple[str, str]]:
    """从单张目标表上下文中提取封装目标 key。"""

    context = target_table_package_context_text(table)
    keys: set[tuple[str, str]] = set()
    for identity in extract_variant_identities_from_text(context):
        keys.add(("identity", normalize_compact(identity)))
    for package_type, pin_count in explicit_package_mentions_from_text(context):
        package_key = package_label_match_key(package_type)
        count = clean_pin_count(pin_count)
        if package_key and count:
            keys.add(("package_pin_count", f"{package_key}:{count}"))
    if not keys:
        for count in explicit_package_pin_counts_from_text(context):
            keys.add(("pin_count", count))
    if not keys:
        for count in standalone_pin_count_mentions_from_text(context):
            keys.add(("pin_count", count))
    return keys


def target_table_package_context_text(table: PackageTargetTable) -> str:
    """拼接目标表附近可用于封装绑定的局部文本。"""

    return "\n".join(
        value
        for value in (
            table.title,
            table.group_context,
            *table.current_chapter_titles,
            " ".join(table.headers),
        )
        if value
    )


def add_target_figure_variant_catalog_entries(
    entries: Sequence[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[PackageCatalogEntry]:
    """从目标引脚表局部图题/表题中补全 H/S/P 等变体槽位。

    第二次模型经常只返回 ``DRV8242-Q1 / VQFN / 20`` 这样的 family 目录，
    但目标表上方图题已经明确写着 ``DRV8242H-Q1``、``DRV8242S-Q1``。
    在多 pin-count 文件中，局部图题是比物理封装名更强的映射证据，因此
    冻结 slot 前把这些变体补成独立目录项；后续绑定仍走统一的 identity
    匹配流程。
    """

    if not target_tables:
        return list(entries)

    variant_evidence: dict[tuple[str, str, str], dict[str, Any]] = {}
    for table in target_tables:
        context = target_table_package_context_text(table)
        variant_identities = extract_variant_identities_from_text(context)
        if len(variant_identities) != 1:
            continue
        variant_identity = variant_identities[0]

        package_type, pin_count = package_metadata_from_variant_context(context)
        evidence_key = target_figure_variant_evidence_key(
            identity_name=variant_identity,
            package_type=package_type,
            pin_count=pin_count,
        )
        evidence = variant_evidence.setdefault(
            evidence_key,
            {
                "identity_name": variant_identity,
                "table_ids": [],
                "package_type": "",
                "pin_count": "",
            },
        )
        if table.table_id not in evidence["table_ids"]:
            evidence["table_ids"].append(table.table_id)

        if package_type and not evidence["package_type"]:
            evidence["package_type"] = package_type
        if pin_count and not evidence["pin_count"]:
            evidence["pin_count"] = pin_count

    if len(variant_evidence) < 2:
        return list(entries)

    derived_entries = [
        PackageCatalogEntry(
            package_key="",
            identity_name=str(evidence["identity_name"]),
            package_type=str(evidence["package_type"]),
            pin_count=str(evidence["pin_count"]),
            evidence_table_ids=list(evidence["table_ids"]),
        )
        for evidence in variant_evidence.values()
    ]
    base_identities = {
        base_identity
        for entry in derived_entries
        for base_identity in [variant_base_identity_from_identity(entry.identity_name)]
        if base_identity
    }

    result = entries_without_umbrella_family_slots(
        list(entries),
        base_identities=base_identities,
        derived_entries=derived_entries,
    )
    for incoming in derived_entries:
        merge_target_figure_variant_catalog_entry(result, incoming)

    if diagnostics is not None:
        diagnostics.append(
            {
                "stage": "target_figure_variant_catalog_entries",
                "status": "applied",
                "base_identities": sorted(base_identities),
                "created_identities": [
                    entry.identity_name for entry in derived_entries
                ],
                "before": len(entries),
                "after": len(result),
            }
        )
    return result


def target_figure_variant_evidence_key(
    *,
    identity_name: str,
    package_type: str,
    pin_count: str,
) -> tuple[str, str, str]:
    """目标图题派生槽按 identity + package + pin_count 保持独立。"""

    return (
        normalize_compact(identity_name),
        package_label_match_key(package_type),
        clean_pin_count(pin_count),
    )


def merge_target_figure_variant_catalog_entry(
    entries: list[PackageCatalogEntry],
    incoming: PackageCatalogEntry,
) -> None:
    """合并目标图题派生项，但不合并同 identity 的不同封装映射。"""

    incoming_key = target_figure_variant_evidence_key(
        identity_name=incoming.identity_name,
        package_type=incoming.package_type,
        pin_count=incoming.pin_count,
    )
    for existing in entries:
        existing_key = target_figure_variant_evidence_key(
            identity_name=existing.identity_name,
            package_type=existing.package_type,
            pin_count=existing.pin_count,
        )
        if existing_key != incoming_key:
            continue
        merge_redundant_catalog_evidence(existing, incoming)
        return
    entries.append(incoming)


def add_target_figure_physical_catalog_entries(
    entries: Sequence[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[PackageCatalogEntry]:
    """从目标表上方封装图题补建物理槽位。

    这只处理 ``DGS Package / 10-Pin VSSOP``、``YZF Package9-Pin DSBGA``
    这类图题证据。它不依赖 PDF 文件名；且必须至少在目标表中形成两个
    不同物理槽位，才会覆盖第二次模型给出的可疑目录。
    """

    if not target_tables:
        return list(entries)

    evidence_by_signature: dict[tuple[str, str, str], dict[str, Any]] = {}
    for table in target_tables:
        mentions = target_physical_metadata_mentions_from_text(
            target_table_package_context_text(table)
        )
        # 只有当前目标表自身能唯一指向一个封装图题时才作为 must-link。
        if len(mentions) != 1:
            continue
        package_type, package_drawing, pin_count = mentions[0]
        if not (package_type and package_drawing and pin_count):
            continue
        signature = (
            package_label_match_key(package_type),
            normalize_compact(package_drawing),
            clean_pin_count(pin_count),
        )
        if not all(signature):
            continue
        evidence = evidence_by_signature.setdefault(
            signature,
            {
                "package_type": package_type,
                "package_drawing": package_drawing,
                "pin_count": pin_count,
                "table_ids": [],
            },
        )
        if table.table_id not in evidence["table_ids"]:
            evidence["table_ids"].append(table.table_id)

    if len(evidence_by_signature) < 2:
        return list(entries)

    derived_entries = [
        PackageCatalogEntry(
            package_key="",
            package_type=str(evidence["package_type"]),
            package_drawing=str(evidence["package_drawing"]),
            pin_count=str(evidence["pin_count"]),
            evidence_table_ids=list(evidence["table_ids"]),
        )
        for evidence in evidence_by_signature.values()
    ]

    result = entries_without_conflicting_target_physical_slots(
        list(entries),
        derived_entries=derived_entries,
    )
    for incoming in derived_entries:
        merge_target_physical_catalog_entry(result, incoming)

    if diagnostics is not None:
        diagnostics.append(
            {
                "stage": "target_figure_physical_catalog_entries",
                "status": "applied",
                "before": len(entries),
                "after": len(result),
                "created_physical_slots": [
                    {
                        "package_type": entry.package_type,
                        "package_drawing": entry.package_drawing,
                        "pin_count": entry.pin_count,
                        "evidence_table_ids": list(entry.evidence_table_ids),
                    }
                    for entry in derived_entries
                ],
            }
        )
    return result


def target_physical_metadata_mentions_from_text(
    text: str,
) -> list[tuple[str, str, str]]:
    """提取目标图题中的 ``(package_type, drawing, pin_count)``。"""

    value = re.sub(r"\s+", " ", str(text or ""))
    package_pattern = PACKAGE_FAMILY_PATTERN
    patterns = [
        # YZF Package9-Pin DSBGA, DGS Package 10-Pin VSSOP
        rf"(?<![A-Za-z0-9])"
        rf"(?P<drawing>[A-Z0-9]{{2,8}})[ \t]+Package[ \t]*"
        rf"(?P<count>\d{{1,4}})[ \t]*[- ]?[ \t]*pin(?:s)?[ \t]+"
        rf"(?P<pkg>{package_pattern}(?:[- ][A-Za-z0-9]+)?)(?![A-Za-z0-9])",
    ]
    result: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            item = (
                clean_public_package_name(match.group("pkg")),
                clean_metadata(match.group("drawing")),
                clean_pin_count(match.group("count")),
            )
            key = (
                package_label_match_key(item[0]),
                normalize_compact(item[1]),
                item[2],
            )
            if all(key) and key not in seen:
                seen.add(key)
                result.append(item)
    return result


def entries_without_conflicting_target_physical_slots(
    entries: Sequence[PackageCatalogEntry],
    *,
    derived_entries: Sequence[PackageCatalogEntry],
) -> list[PackageCatalogEntry]:
    """目标图题已形成 must-link 时，移除与其冲突的旧物理目录项。"""

    result: list[PackageCatalogEntry] = []
    for entry in entries:
        if any(
            target_physical_slot_conflicts(entry, derived)
            for derived in derived_entries
        ):
            continue
        result.append(entry)
    return result


def target_physical_slot_conflicts(
    entry: PackageCatalogEntry,
    derived: PackageCatalogEntry,
) -> bool:
    """两项至少两项物理字段相同但整体不同，说明旧目录项被错配。"""

    entry_signature = canonical_physical_metadata(entry)
    derived_signature = canonical_physical_metadata(derived)
    if not all(derived_signature):
        return False
    if entry_signature == derived_signature:
        return False
    equal_fields = sum(
        1
        for entry_value, derived_value in zip(entry_signature, derived_signature)
        if entry_value and derived_value and entry_value == derived_value
    )
    return equal_fields >= 2


def merge_target_physical_catalog_entry(
    entries: list[PackageCatalogEntry],
    incoming: PackageCatalogEntry,
) -> None:
    """按完整物理签名合并目标图题派生槽位。"""

    incoming_signature = canonical_physical_metadata(incoming)
    for existing in entries:
        if canonical_physical_metadata(existing) != incoming_signature:
            continue
        merge_redundant_catalog_evidence(existing, incoming)
        return
    entries.append(incoming)


def consolidate_target_physical_catalog_entries(
    entries: Sequence[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[PackageCatalogEntry]:
    """合并订购型号造成的重复物理槽，并删除被强槽覆盖的弱槽。"""

    if not entries:
        return []

    target_context = "\n".join(
        target_table_package_context_text(table) for table in target_tables
    )
    groups: dict[tuple[str, str, str], list[PackageCatalogEntry]] = {}
    for entry in entries:
        signature = canonical_physical_metadata(entry)
        if all(signature):
            groups.setdefault(signature, []).append(entry)

    skipped_entry_ids: set[int] = set()
    merged_signatures: list[tuple[str, str, str]] = []
    for signature, group in groups.items():
        if len(group) < 2:
            continue
        if should_keep_duplicate_physical_slots(group, target_context):
            continue
        representative = group[0]
        for incoming in group[1:]:
            merge_redundant_catalog_evidence(representative, incoming)
            skipped_entry_ids.add(id(incoming))
        merged_signatures.append(signature)

    strong_entries = [
        entry
        for entry in entries
        if id(entry) not in skipped_entry_ids
        and all(canonical_physical_metadata(entry))
    ]
    removed_weak_entries: list[dict[str, Any]] = []
    result: list[PackageCatalogEntry] = []
    for entry in entries:
        if id(entry) in skipped_entry_ids:
            continue
        if (
            not all(canonical_physical_metadata(entry))
            and weak_entry_is_covered_by_strong_physical_slot(entry, strong_entries)
            and not catalog_entry_identity_is_mentioned(entry, target_context)
        ):
            removed_weak_entries.append(catalog_entry_debug_dict(entry))
            continue
        result.append(entry)

    if diagnostics is not None and (
        merged_signatures or removed_weak_entries or len(result) != len(entries)
    ):
        diagnostics.append(
            {
                "stage": "target_physical_catalog_consolidation",
                "status": "applied",
                "before": len(entries),
                "after": len(result),
                "merged_physical_signatures": [
                    list(signature) for signature in merged_signatures
                ],
                "removed_weak_entries": removed_weak_entries,
            }
        )
    return result


def should_keep_duplicate_physical_slots(
    entries: Sequence[PackageCatalogEntry],
    target_context: str,
) -> bool:
    """同物理封装只有在目标表明确提到多个身份时才保留多个映射槽。"""

    mentioned_identity_keys = {
        normalize_compact(name)
        for entry in entries
        for name in [entry.identity_name, *entry.identity_aliases]
        if name and identity_name_in_text(name, target_context)
    }
    return len(mentioned_identity_keys) >= 2


def weak_entry_is_covered_by_strong_physical_slot(
    entry: PackageCatalogEntry,
    strong_entries: Sequence[PackageCatalogEntry],
) -> bool:
    """弱目录项的已知物理字段完全落在某个强目录项内时可删除。"""

    package_key, drawing_key, pin_count = canonical_physical_metadata(entry)
    if not package_key:
        return False
    known_fields = [
        (0, package_key),
        (1, drawing_key),
        (2, pin_count),
    ]
    known_fields = [(index, value) for index, value in known_fields if value]
    if not known_fields:
        return False
    for strong_entry in strong_entries:
        strong_signature = canonical_physical_metadata(strong_entry)
        if not all(strong_signature):
            continue
        if all(strong_signature[index] == value for index, value in known_fields):
            return True
    return False


def catalog_entry_identity_is_mentioned(
    entry: PackageCatalogEntry,
    text: str,
) -> bool:
    """判断目录项身份或别名是否在目标表局部文本中出现。"""

    return any(
        identity_name_in_text(name, text)
        for name in [entry.identity_name, *entry.identity_aliases]
        if name
    )


def catalog_entry_debug_dict(entry: PackageCatalogEntry) -> dict[str, Any]:
    """用于诊断输出的精简目录项。"""

    return {
        "identity_name": entry.identity_name,
        "identity_aliases": list(entry.identity_aliases),
        "package_type": entry.package_type,
        "package_drawing": entry.package_drawing,
        "pin_count": entry.pin_count,
        "public_label": entry.public_label,
        "evidence_table_ids": list(entry.evidence_table_ids),
    }


def extract_variant_identities_from_text(text: str) -> list[str]:
    """从目标表局部文本中直接提取 ``基础型号+单字母变体-后缀``。

    例：``DRV8145H-Q1``、``DRV8145S -Q1``。基础型号来自 PDF 文本本身，
    不是文件名；只有前缀以数字结尾且变体为单个大写字母时才接受，避免把
    普通型号误拆成变体。
    """

    result: list[str] = []
    seen: set[str] = set()
    pattern = (
        r"(?<![A-Za-z0-9])"
        r"(?P<prefix>[A-Za-z][A-Za-z0-9]{2,20}\d)"
        r"(?P<variant>[A-Z])\s*[-–—]\s*"
        r"(?P<suffix>[A-Za-z0-9]{1,8})"
        r"(?![A-Za-z0-9])"
    )
    for match in re.finditer(pattern, str(text or "")):
        identity = (
            f"{match.group('prefix')}{match.group('variant')}-"
            f"{match.group('suffix')}"
        )
        identity = clean_identity_name(identity)
        key = normalize_compact(identity)
        if identity and key not in seen:
            seen.add(key)
            result.append(identity)
    return result


def variant_base_identity_from_identity(identity_name: str) -> str:
    """把 ``DRV8145H-Q1`` 还原成 family umbrella ``DRV8145-Q1``。"""

    match = re.fullmatch(
        r"(?P<prefix>[A-Za-z][A-Za-z0-9]{2,20}\d)"
        r"(?P<variant>[A-Z])-(?P<suffix>[A-Za-z0-9]{1,8})",
        str(identity_name or ""),
    )
    if not match:
        return ""
    return clean_identity_name(f"{match.group('prefix')}-{match.group('suffix')}")


def package_metadata_from_variant_context(text: str) -> tuple[str, str]:
    """从同一局部文本中抽取封装族和 pin_count。"""

    package_type = ""
    pin_count = ""
    for candidate_package, candidate_count in explicit_package_mentions_from_text(text):
        if candidate_package and not package_type:
            package_type = clean_public_package_name(candidate_package)
        if candidate_count and not pin_count:
            pin_count = clean_pin_count(candidate_count)
        if package_type and pin_count:
            break
    if not pin_count:
        counts = explicit_package_pin_counts_from_text(text)
        if len(counts) == 1:
            pin_count = next(iter(counts))
    if not package_type:
        package_type = clean_public_package_name(text)
    return package_type, pin_count


def explicit_package_mentions_from_text(text: str) -> list[tuple[str, str]]:
    """返回局部文本中明确互相绑定的 ``(package_type, pin_count)``。"""

    value = str(text or "")
    package_pattern = PACKAGE_FAMILY_PATTERN
    patterns = [
        # VQFN (20), VQFN-HR (14), HVSSOP (28)
        rf"(?<![A-Za-z0-9])(?P<pkg>{package_pattern}(?:[- ][A-Za-z0-9]+)?)[ \t]*[\(（][ \t]*(?P<count>\d{{1,4}})[ \t]*[\)）]",
        # 20-Pin VQFN, 20 pin VQFN
        rf"(?<![A-Za-z0-9])(?P<count>\d{{1,4}})[ \t]*[- ]?[ \t]*pin(?:s)?[ \t]+(?P<pkg>{package_pattern}(?:[- ][A-Za-z0-9]+)?)(?![A-Za-z0-9])",
        # VQFN 20-pin, VQFN 20 pin
        rf"(?<![A-Za-z0-9])(?P<pkg>{package_pattern}(?:[- ][A-Za-z0-9]+)?)[ \t]+(?P<count>\d{{1,4}})[ \t]*[- ]?[ \t]*pin(?:s)?(?![A-Za-z0-9])",
        # QFN 32 Pin Functions, BGA 64 package
        rf"(?<![A-Za-z0-9])(?P<pkg>{package_pattern})[ \t]+(?P<count>\d{{1,4}})[ \t]+(?:pin|pins|package|pkg)(?![A-Za-z0-9])",
        # QFN 32, BGA 64
        rf"(?<![A-Za-z0-9])(?P<pkg>{package_pattern})[ \t]+(?P<count>\d{{1,4}})(?![A-Za-z0-9])",
    ]
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            item = (
                re.sub(r"\s+", " ", match.group("pkg")).strip(),
                clean_pin_count(match.group("count")),
            )
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def standalone_pin_count_mentions_from_text(text: str) -> set[str]:
    """提取局部标题中明确写成 ``36-pin`` / ``36 pin`` 的 pin_count。"""

    result: set[str] = set()
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(?P<count>\d{1,4})\s*[- ]\s*pins?(?![A-Za-z0-9])",
        str(text or ""),
        flags=re.IGNORECASE,
    ):
        count = clean_pin_count(match.group("count"))
        if count:
            result.add(count)
    return result


def entries_without_umbrella_family_slots(
    entries: Sequence[PackageCatalogEntry],
    *,
    base_identities: set[str],
    derived_entries: Sequence[PackageCatalogEntry],
) -> list[PackageCatalogEntry]:
    """派生变体已覆盖目标表时，移除原 family/匿名物理 umbrella 槽。"""

    derived_pin_counts = {
        pin_count
        for entry in derived_entries
        for pin_count in explicit_package_pin_counts_from_entry(entry)
    }
    derived_package_keys = {
        package_label_match_key(entry.package_type)
        for entry in derived_entries
        if package_label_match_key(entry.package_type)
    }
    base_keys = {
        normalize_compact(base_identity)
        for base_identity in base_identities
        if normalize_compact(base_identity)
    }

    result = []
    for entry in entries:
        identity_key = normalize_compact(entry.identity_name)
        entry_pin_counts = explicit_package_pin_counts_from_entry(entry)
        entry_package_key = package_label_match_key(entry.package_type)
        is_base_identity = bool(identity_key and identity_key in base_keys)
        is_anonymous_physical = not identity_key and bool(entry_package_key)
        overlaps_derived_pin_count = bool(
            derived_pin_counts
            and entry_pin_counts
            and not entry_pin_counts.isdisjoint(derived_pin_counts)
        )
        overlaps_derived_package = bool(
            entry_package_key and entry_package_key in derived_package_keys
        )
        if (is_base_identity or is_anonymous_physical) and (
            overlaps_derived_pin_count or overlaps_derived_package
        ):
            continue
        result.append(entry)
    return result


def select_single_package_fallback_entry(
    entries: Sequence[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
) -> PackageCatalogEntry:
    """从误膨胀目录中选择一个单封装槽位，并复制为新的唯一 entry。"""

    selected = max(
        enumerate(entries),
        key=lambda item: single_package_fallback_entry_score(
            item[1],
            entry_index=item[0],
        ),
    )[1]
    evidence_table_ids = list(
        dict.fromkeys(
            [
                *selected.evidence_table_ids,
                *(table.table_id for table in target_tables),
            ]
        )
    )
    result = replace(
        selected,
        package_key="",
        identity_aliases=list(selected.identity_aliases),
        evidence_table_ids=evidence_table_ids,
    )
    return result


def single_package_fallback_entry_score(
    entry: PackageCatalogEntry,
    *,
    entry_index: int,
) -> int:
    """给单封装保底候选打分；只使用目录项自身元数据。"""

    score = 0
    if clean_public_symbol_name(entry.public_label):
        score += 700
    if clean_metadata(entry.package_drawing):
        score += 600
    if clean_pin_count(entry.pin_count):
        score += 400
    if clean_public_package_name(entry.package_type):
        score += 200
    if re.search(
        r"\b(?:reference|evaluation|demo|board|carrier)\b",
        normalize_text(entry.package_type),
    ):
        score -= 5000
    # 稳定打破平局：保持原目录顺序。
    score -= entry_index
    return score


def metadata_contains_pin_count(value: str, pin_count: str) -> bool:
    """判断物理描述里是否独立出现目标 pin_count。"""

    pin_count = clean_pin_count(pin_count)
    if not pin_count:
        return False
    return bool(
        re.search(
            rf"(?<!\d){re.escape(pin_count)}(?!\d)",
            str(value or ""),
        )
    )


def single_package_all_unresolved_fallback_diagnostic(
    *,
    before_entries: Sequence[PackageCatalogEntry],
    selected_entry: PackageCatalogEntry,
    target_tables: Sequence[PackageTargetTable],
    initial_binding_diagnostics: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """记录单封装兜底触发原因和收敛结果。"""

    return {
        "stage": "single_package_all_unresolved_fallback",
        "status": "applied",
        "reason": "all_package_bindings_unresolved",
        "before": len(before_entries),
        "after": 1,
        "target_table_ids": [table.table_id for table in target_tables],
        "initial_unresolved_tables": [
            diagnostic.get("table_id")
            for diagnostic in initial_binding_diagnostics
            if diagnostic.get("stage") == "package_binding"
        ],
        "document_packages_before": [
            assignment_from_entry(entry, before_entries, reason="diagnostic").pkg
            for entry in before_entries
        ],
        "selected_package": assignment_from_entry(
            selected_entry,
            [selected_entry],
            reason="diagnostic",
        ).pkg,
        "selected_entry": {
            "package_type": selected_entry.package_type,
            "package_drawing": selected_entry.package_drawing,
            "pin_count": selected_entry.pin_count,
            "public_label": selected_entry.public_label,
        },
    }


def resolve_table_symbol_package_links(
    table: PackageTargetTable,
    local_labels: Sequence[str],
    entries: Sequence[PackageCatalogEntry],
) -> SymbolPackageLinkResolution:
    """从表题和表头解析 Symbol(Package) 关系，修正局部分支标签。

    典型场景是表内列名只有 ``QFN24/QFN40``，但表题写着
    ``RKP (QFN40) and RGE (QFN24)``。此时真正用于绑定目录槽位的应该是
    ``RGE/RKP``，不是局部物理封装文本本身。
    """

    if len(local_labels) < 2:
        return SymbolPackageLinkResolution(tuple(local_labels))

    link_candidates = collect_symbol_package_link_candidates(
        table,
        local_labels,
        entries,
    )
    if not link_candidates:
        return SymbolPackageLinkResolution(tuple(local_labels))

    selected_by_slot: dict[int, tuple[str, tuple[str, ...]]] = {}
    conflicts: list[dict[str, Any]] = []

    for local_slot, local_label in enumerate(local_labels):
        package_key = package_label_match_key(local_label)
        if not package_key:
            continue
        candidates = link_candidates.get(package_key, {})
        if not candidates:
            continue

        usable: list[tuple[str, str, tuple[str, ...]]] = []
        for symbol_key, record in candidates.items():
            symbol = str(record["symbol"])
            # must-link 只有在 symbol 能唯一命中文档级槽位时才生效；否则不
            # 替代原标签，避免把一条弱字符串关系变成硬绑定。
            if len(_entries_matching_exact_branch_label(entries, symbol)) != 1:
                continue
            usable.append(
                (
                    symbol_key,
                    symbol,
                    tuple(sorted(str(source) for source in record["sources"])),
                )
            )

        if len(usable) > 1:
            conflicts.append(
                {
                    "local_slot": local_slot,
                    "local_label": local_label,
                    "candidate_symbols": [symbol for _key, symbol, _sources in usable],
                    "reason": "multiple_symbols_for_package_label",
                }
            )
            continue
        if len(usable) == 1:
            _symbol_key, symbol, sources = usable[0]
            selected_by_slot[local_slot] = (symbol, sources)

    symbol_to_slots: dict[str, list[int]] = {}
    for local_slot, (symbol, _sources) in selected_by_slot.items():
        symbol_to_slots.setdefault(normalize_compact(symbol), []).append(local_slot)
    for symbol_key, slots in symbol_to_slots.items():
        if len(slots) <= 1:
            continue
        conflicts.append(
            {
                "local_slots": slots,
                "candidate_symbol": selected_by_slot[slots[0]][0],
                "candidate_symbol_key": symbol_key,
                "reason": "same_symbol_for_multiple_package_labels",
            }
        )

    if conflicts:
        return SymbolPackageLinkResolution(
            tuple(local_labels),
            conflicts=tuple(conflicts),
        )

    effective_labels = list(local_labels)
    sources_by_slot: dict[int, tuple[str, ...]] = {}
    for local_slot, (symbol, sources) in selected_by_slot.items():
        effective_labels[local_slot] = symbol
        sources_by_slot[local_slot] = sources

    return SymbolPackageLinkResolution(
        tuple(effective_labels),
        sources_by_slot=sources_by_slot,
    )


def collect_symbol_package_link_candidates(
    table: PackageTargetTable,
    local_labels: Sequence[str],
    entries: Sequence[PackageCatalogEntry],
) -> dict[str, dict[str, dict[str, Any]]]:
    """收集表题和表头中的 package label -> symbol 候选。"""

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for source, text in (
        ("table_title", table.title),
        ("table_header", " ".join(table.headers)),
    ):
        for symbol, package_label in extract_symbol_package_pairs(text):
            add_symbol_package_link_candidate(
                result,
                package_label=package_label,
                symbol=symbol,
                source=source,
            )

    # 有些 PDF 的列头写成 ``QFN24 RGE Pin No.`` 或 ``QFN24 (RGE)``，
    # 不满足严格 Symbol(Package) 顺序。只在同一个 header 单元格内同时出现
    # 一个局部 package label 和一个已知短 symbol 时，才补充 header must-link。
    symbol_candidates = entry_symbol_candidates(entries)
    for header in table.headers:
        for local_label in local_labels:
            if not text_contains_package_label(header, local_label):
                continue
            for symbol in symbol_candidates:
                if normalize_compact(symbol) == normalize_compact(local_label):
                    continue
                if package_name_in_text(symbol, normalize_text(header)):
                    add_symbol_package_link_candidate(
                        result,
                        package_label=local_label,
                        symbol=symbol,
                        source="table_header",
                    )
    return result


def extract_symbol_package_pairs(text: str) -> list[tuple[str, str]]:
    """提取 ``SYMBOL (PACKAGE)`` 形式的短标签关系。"""

    pairs: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(?P<symbol>[A-Za-z][A-Za-z0-9-]{0,14})"
        r"\s*[\(（]\s*(?P<package>[^()（）]{1,40})\s*[\)）]",
        str(text or ""),
    ):
        symbol = clean_public_symbol_name(match.group("symbol"))
        package_label = clean_metadata(match.group("package"))
        if symbol and package_label_match_key(package_label):
            pairs.append((symbol, package_label))
    return pairs


def add_symbol_package_link_candidate(
    result: dict[str, dict[str, dict[str, Any]]],
    *,
    package_label: str,
    symbol: str,
    source: str,
) -> None:
    """添加一个 package label -> symbol 候选，并合并来源。"""

    package_key = package_label_match_key(package_label)
    symbol = clean_public_symbol_name(symbol)
    symbol_key = normalize_compact(symbol)
    if not package_key or not symbol_key:
        return
    package_bucket = result.setdefault(package_key, {})
    record = package_bucket.setdefault(
        symbol_key,
        {"symbol": symbol, "sources": set()},
    )
    record["sources"].add(source)


def entry_symbol_candidates(
    entries: Sequence[PackageCatalogEntry],
) -> tuple[str, ...]:
    """收集目录项中可用于表头 must-link 的短 symbol 候选。"""

    result: dict[str, str] = {}
    for entry in entries:
        for value in [
            entry.public_label,
            *entry.identity_aliases,
            entry.package_drawing,
        ]:
            symbol = clean_public_symbol_name(value)
            if symbol:
                result.setdefault(normalize_compact(symbol), symbol)
    return tuple(result.values())


def text_contains_package_label(text: str, package_label: str) -> bool:
    """判断一个 header 单元格是否包含指定局部 package 标签。"""

    package_key = package_label_match_key(package_label)
    if not package_key:
        return False
    if package_name_in_text(package_label, normalize_text(text)):
        return True
    return package_label_match_key(text) == package_key


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
            side_effective_labels = normalize_package_side_local_labels(
                table,
                local_labels,
            )
            if side_effective_labels is not None:
                for local_slot, (local_label, effective_label) in enumerate(
                    zip(local_labels, side_effective_labels)
                ):
                    matches = match_entries_in_text(entries, effective_label)
                    if len(matches) == 1:
                        assignment = assignment_from_entry(
                            matches[0],
                            entries,
                            reason="package_side_label",
                        )
                    else:
                        assignment = None
                    reason = (
                        assignment.reason
                        if assignment is not None
                        else (
                            "ambiguous_package_side_label"
                            if len(matches) > 1
                            else "package_side_label_unresolved"
                        )
                    )
                    append_package_binding_diagnostic(
                        diagnostics,
                        table=table,
                        local_slot=local_slot,
                        local_label=local_label,
                        assignment=assignment,
                        reason=reason,
                        matched_entries=matches,
                        entries=entries,
                        effective_label=effective_label,
                    )
                    if assignment is not None:
                        assignments[(table.table_id, local_slot)] = assignment
                if chapter_context_key(table) != previous_context:
                    previous_explicit = None
                    previous_context = ""
                continue

            link_resolution = resolve_table_symbol_package_links(
                table,
                local_labels,
                entries,
            )
            if link_resolution.conflicts:
                for local_slot, local_label in enumerate(local_labels):
                    append_package_binding_diagnostic(
                        diagnostics,
                        table=table,
                        local_slot=local_slot,
                        local_label=local_label,
                        assignment=None,
                        reason="symbol_package_link_conflict",
                        matched_entries=[],
                        entries=entries,
                        link_conflicts=link_resolution.conflicts,
                    )
                if chapter_context_key(table) != previous_context:
                    previous_explicit = None
                    previous_context = ""
                continue

            effective_labels = list(link_resolution.effective_labels)
            if len(entries) < len(effective_labels):
                # 这张表已经被确认有多个局部分支，但文档级目录槽位不足以做
                # 一对一绑定。这里必须保守跳过该表，不能硬崩批量任务，也不能
                # 复用槽位误绑到错误 pkg。
                for local_slot, local_label in enumerate(local_labels):
                    effective_label = (
                        effective_labels[local_slot]
                        if local_slot < len(effective_labels)
                        else None
                    )
                    append_package_binding_diagnostic(
                        diagnostics,
                        table=table,
                        local_slot=local_slot,
                        local_label=local_label,
                        assignment=None,
                        reason="package_catalog_slot_shortage",
                        matched_entries=[],
                        entries=entries,
                        effective_label=effective_label,
                        link_sources=link_resolution.sources_by_slot.get(
                            local_slot,
                            (),
                        ),
                    )
                if chapter_context_key(table) != previous_context:
                    previous_explicit = None
                    previous_context = ""
                continue

            bound_entries = bind_multi_package_entries(entries, effective_labels)
            for local_slot, (local_label, entry) in enumerate(
                zip(local_labels, bound_entries)
            ):
                assignment = assignment_from_entry(
                    entry,
                    entries,
                    reason="multi_package_global_unique_binding",
                )
                assignments[(table.table_id, local_slot)] = assignment
                append_package_binding_diagnostic(
                    diagnostics,
                    table=table,
                    local_slot=local_slot,
                    local_label=local_label,
                    assignment=assignment,
                    reason=assignment.reason,
                    matched_entries=[entry],
                    entries=entries,
                    effective_label=effective_labels[local_slot],
                    link_sources=link_resolution.sources_by_slot.get(
                        local_slot,
                        (),
                    ),
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
                plan is not None
                and plan.is_multi_package
                and len(local_labels) == len(entries)
            ):
                # 表内已经严格确认有 N 个封装编号列，文档目录也有 N 个槽位。
                # 在标签无法匹配时仍可按稳定列顺序一一对应，数量不发生变化。
                entry = entries[local_slot]
                assignment = assignment_from_entry(
                    entry,
                    entries,
                    reason="multi_package_positional_binding",
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
    effective_label: str | None = None,
    link_sources: Sequence[str] = (),
    link_conflicts: Sequence[dict[str, Any]] = (),
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
    if effective_label is not None and effective_label != local_label:
        diagnostic["effective_label"] = effective_label
    if link_sources:
        diagnostic["symbol_package_link_sources"] = list(link_sources)
    if link_conflicts:
        diagnostic["symbol_package_link_conflicts"] = list(link_conflicts)
    diagnostics.append(diagnostic)


def bind_multi_package_entries(
    entries: Sequence[PackageCatalogEntry],
    local_labels: Sequence[str],
) -> list[PackageCatalogEntry]:
    """把一张表的全部本地分支一次性绑定到互不重复的文档槽位。

    绑定是一个小规模最大权重匹配问题。分支标签与目录项之间的型号、封装族、
    drawing 和显式 pin_count 共同计分；无证据时才用列顺序打破平局。无论
    得分高低，同一张多封装表内都禁止两个分支复用同一个 ``package_key``。
    """

    if len(entries) < len(local_labels):
        return []

    score_matrix = [
        [
            multi_package_binding_score(label, entry, local_slot, entry_slot)
            for entry_slot, entry in enumerate(entries)
        ]
        for local_slot, label in enumerate(local_labels)
    ]
    selected_slots = maximum_weight_unique_assignment(score_matrix)
    return [entries[entry_slot] for entry_slot in selected_slots]


def normalize_package_side_local_labels(
    table: PackageTargetTable,
    local_labels: Sequence[str],
) -> tuple[str, ...] | None:
    """把 BOTTOM/TOP 这类封装面标签归一到真实 package label。

    例如 ``BOTTOM CBP Pkg.`` 和 ``TOP CBP Pkg.`` 都表示 CBP 封装的不同面，
    不是两个独立 symbol。若表头只有 ``BOTTOM``/``TOP``，则从表题/表上下文的
    ``(CBP Pkg.)`` 中补出 CBP。
    """

    if not local_labels:
        return None

    context_label = package_side_context_label(table)
    normalized: list[str] = []
    changed = False
    saw_side_label = False
    for label in local_labels:
        side_label = package_label_from_side_local_label(label)
        if side_label:
            normalized.append(side_label)
            changed = changed or side_label != label
            saw_side_label = True
            continue
        if is_bare_package_side_label(label) and context_label:
            normalized.append(context_label)
            changed = True
            saw_side_label = True
            continue
        normalized.append(label)

    if not saw_side_label or not changed:
        return None
    return tuple(normalized)


def package_label_from_side_local_label(value: str) -> str:
    """从 ``BOTTOM CBP Pkg.`` / ``TOP CBC Pkg. 2`` 中提取 CBP/CBC。"""

    match = re.fullmatch(
        r"\s*(?:bottom|top)\b\s+(?P<body>.*?)\s*",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    body = clean_metadata(match.group("body"))
    body = re.sub(
        r"(?<![A-Za-z0-9])(?:pkg|package)(?![A-Za-z0-9])\.?",
        " ",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"\s*[\(（]?\s*\d+\s*[\)）]?\s*$", "", body)
    body = re.sub(r"\s+", " ", body).strip(" \t\r\n,;:.")
    return body if package_side_label_is_usable(body) else ""


def is_bare_package_side_label(value: str) -> bool:
    """判断标签是否只是 BOTTOM/TOP，不含真实 package 名称。"""

    return bool(re.fullmatch(r"\s*(?:bottom|top)\s*", str(value or ""), re.IGNORECASE))


def package_side_context_label(table: PackageTargetTable) -> str:
    """从当前表题/上下文中提取唯一的 ``CBP Pkg.`` 这类 package label。"""

    labels: dict[str, str] = {}
    for text in [
        table.title,
        table.group_context,
        *table.current_chapter_titles,
        *table.headers,
    ]:
        for label in package_labels_from_pkg_text(text):
            labels.setdefault(normalize_compact(label), label)
    return next(iter(labels.values())) if len(labels) == 1 else ""


def package_labels_from_pkg_text(text: str) -> tuple[str, ...]:
    """提取文本里的短 package label，例如 ``(CBP Pkg.)``。"""

    result: dict[str, str] = {}
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(?P<label>[A-Za-z][A-Za-z0-9-]{0,14})"
        r"\s*(?:pkg|package)\.?(?![A-Za-z0-9])",
        str(text or ""),
        flags=re.IGNORECASE,
    ):
        label = clean_metadata(match.group("label"))
        if package_side_label_is_usable(label):
            result.setdefault(normalize_compact(label), label)
    return tuple(result.values())


def package_side_label_is_usable(value: str) -> bool:
    """封装面归一化只接受短 package/drawing 标签，拒绝长描述。"""

    value = clean_metadata(value)
    return bool(
        value
        and len(value) <= 15
        and "|" not in value
        and "\n" not in value
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,14}", value)
        and not is_bare_package_side_label(value)
    )


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
    local_slot: int,
    entry_slot: int,
) -> int:
    """计算一个分支标签与一个目录槽位之间的确定性关联分数。"""

    label_text = normalize_text(local_label)
    label_compact = normalize_compact(local_label)
    score = 0

    identities = [entry.identity_name, *entry.identity_aliases, entry.public_label]
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

    label_pin_count = explicit_pin_count_from_label(local_label)
    entry_pin_count = clean_pin_count(entry.pin_count)
    if label_pin_count and entry_pin_count:
        score += 400 if label_pin_count == entry_pin_count else -500

    # 结构证据完全相同时才依赖稳定列顺序。该分值远低于任何语义证据。
    score += max(0, 20 - abs(local_slot - entry_slot))
    return score


def explicit_pin_count_from_label(value: str) -> str:
    """只读取带 PIN 文字的显式引脚数，避免把型号内部数字当作数量。"""

    match = re.search(
        r"(?<![A-Za-z0-9])(\d+)\s*[- ]?\s*pin(?:s)?(?![A-Za-z0-9])",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return clean_pin_count(match.group(1)) if match else ""


def match_entries_in_text(
    entries: Sequence[PackageCatalogEntry],
    text: str,
) -> list[PackageCatalogEntry]:
    """按完整名称边界匹配目录，不执行编辑距离或前缀猜测。"""

    normalized_text = normalize_text(text)
    text_pin_counts = explicit_package_pin_counts_from_text(text)
    text_pin_counts.update(standalone_pin_count_mentions_from_text(text))
    identity_matches = []
    for entry in entries:
        # 器件型号只用于内部关联；封装类型和 Drawing 也可参与绑定。若同一
        # 元数据对应多个槽位，会保持歧义，不能据此合并槽位。
        identity_names = [entry.identity_name, *entry.identity_aliases]
        if any(identity_name_in_text(name, text) for name in identity_names):
            identity_matches.append(entry)
    if identity_matches:
        return filter_identity_matches_by_package_context(
            identity_matches,
            text,
            text_pin_counts=text_pin_counts,
        )

    matches = []
    for entry in entries:
        names = []
        if entry.public_label:
            names.append(entry.public_label)
        if entry.package_type:
            names.append(entry.package_type)
        if entry.package_drawing:
            names.append(entry.package_drawing)
        if any(package_name_in_text(name, normalized_text) for name in names):
            entry_pin_counts = explicit_package_pin_counts_from_entry(entry)
            if text_pin_counts and text_pin_counts.isdisjoint(entry_pin_counts):
                continue
            matches.append(entry)
    if matches:
        return matches

    if text_pin_counts:
        return [
            entry
            for entry in entries
            if not text_pin_counts.isdisjoint(
                explicit_package_pin_counts_from_entry(entry)
            )
        ]
    return []


def filter_identity_matches_by_package_context(
    matches: Sequence[PackageCatalogEntry],
    text: str,
    *,
    text_pin_counts: set[str],
) -> list[PackageCatalogEntry]:
    """identity 命中多个槽时，用同一文本中的封装/引脚数消歧。"""

    result = list(matches)
    mentioned_package_keys = {
        package_label_match_key(package_type)
        for package_type, _pin_count in explicit_package_mentions_from_text(text)
        if package_label_match_key(package_type)
    }
    if mentioned_package_keys:
        package_filtered = [
            entry
            for entry in result
            if package_label_match_key(entry.package_type) in mentioned_package_keys
        ]
        if package_filtered:
            result = package_filtered

    if text_pin_counts:
        pin_filtered = [
            entry
            for entry in result
            if not text_pin_counts.isdisjoint(
                explicit_package_pin_counts_from_entry(entry)
            )
        ]
        if pin_filtered:
            result = pin_filtered
    return result


def explicit_package_pin_counts_from_entry(entry: PackageCatalogEntry) -> set[str]:
    """收集目录项物理元数据中明确写出的 pin_count。"""

    result: set[str] = set()
    pin_count = clean_pin_count(entry.pin_count)
    if pin_count:
        result.add(pin_count)
    for value in (
        entry.public_label,
        entry.package_type,
        entry.package_drawing,
    ):
        result.update(explicit_package_pin_counts_from_text(value))
    return result


def explicit_package_pin_counts_from_text(value: str) -> set[str]:
    """从封装上下文中提取明确跟封装名绑定的 pin_count。"""

    text = str(value or "")
    result: set[str] = set()
    package_pattern = PACKAGE_FAMILY_PATTERN
    patterns = [
        # VQFN (20), VQFN-HR (14), HVSSOP (28)
        rf"(?<![A-Za-z0-9]){package_pattern}(?:[- ][A-Za-z0-9]+)?[ \t]*[\(（][ \t]*(\d{{1,4}})[ \t]*[\)）]",
        # 20-Pin VQFN, 20 pin VQFN
        rf"(?<![A-Za-z0-9])(\d{{1,4}})[ \t]*[- ]?[ \t]*pin(?:s)?[ \t]+{package_pattern}(?![A-Za-z0-9])",
        # VQFN 20-pin, VQFN 20 pin
        rf"(?<![A-Za-z0-9]){package_pattern}(?:[- ][A-Za-z0-9]+)?[ \t]+(\d{{1,4}})[ \t]*[- ]?[ \t]*pin(?:s)?(?![A-Za-z0-9])",
        # QFN 32 Pin Functions, BGA 64 package
        rf"(?<![A-Za-z0-9]){package_pattern}[ \t]+(\d{{1,4}})[ \t]+(?:pin|pins|package|pkg)(?![A-Za-z0-9])",
        # QFN 32, BGA 64
        rf"(?<![A-Za-z0-9]){package_pattern}[ \t]+(\d{{1,4}})(?![A-Za-z0-9])",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            pin_count = clean_pin_count(match.group(1))
            if pin_count:
                result.add(pin_count)
    return result


def identity_name_in_text(name: str, text: str) -> bool:
    """匹配器件身份；额外支持 ``CC430F614x`` 这类 family wildcard。"""

    normalized_text = normalize_text(text)
    if package_name_in_text(name, normalized_text):
        return True
    return identity_family_wildcard_in_text(name, text)


def identity_family_wildcard_in_text(name: str, text: str) -> bool:
    """判断局部标题中的 ``...x`` family token 是否覆盖具体型号。"""

    identity = normalize_compact(clean_identity_name(name))
    if not identity:
        identity = normalize_compact(name)
    if not identity:
        return False
    for token in re.findall(
        r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9-]{3,20}[xX])(?![A-Za-z0-9])",
        str(text or ""),
    ):
        prefix = normalize_compact(token[:-1])
        if len(prefix) < 4:
            continue
        if len(identity) > len(prefix) and identity.startswith(prefix):
            return True
    return False


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
        0,
    )
    return PackageAssignment(
        package_key=entry.package_key,
        pkg=(
            clean_public_symbol_name(entry.public_label)
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


def clean_public_symbol_name(value: str) -> str:
    """清理已确认的公开短 symbol，拒绝物理封装族和拼接结果。"""

    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,;:")
    # 表头脚注不是 symbol 本体，例如 RKP(1) 应按 RKP 处理。
    value = re.sub(r"\s*[\(（]\s*\d+\s*[\)）]\s*$", "", value)
    if (
        not value
        or "|" in value
        or "\n" in value
        or len(value) > 15
        or is_generic_package_label(value)
        or is_physical_package_public_label(value)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,14}", value)
    ):
        return ""
    return value


def is_physical_package_public_label(value: str) -> bool:
    """判断短文本是否只是 QFN24/VQFN/BGA 这类物理封装标签。"""

    compact = normalize_compact(value)
    if PHYSICAL_PACKAGE_LABEL_COMPACT_RE.fullmatch(compact):
        return True
    package_key = package_label_match_key(value)
    return bool(
        package_key
        and PHYSICAL_PACKAGE_LABEL_COMPACT_RE.fullmatch(package_key)
    )


def extract_public_package_name_from_metadata(value: str) -> str:
    """从长包装描述中截取明确的物理封装族名称。

    这里只识别数据手册常见封装族及其紧邻的编号/E-PAD 后缀。尺寸、器件型号、
    Pb-Free 等其余文字都不会进入公开 pkg，也不会参与槽位数量判断。
    """

    match = re.search(
        rf"(?<![A-Za-z0-9])({PACKAGE_FAMILY_PATTERN}(?:[- ]\d+)?(?:\s+E-?PAD)?)(?![A-Za-z0-9])",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def clean_package_name(value: str) -> str:
    """兼容旧调用名称；当前语义等同于清理公开物理封装名。"""

    return clean_public_package_name(value)


def package_label_match_key(value: str) -> str:
    """把 QFN24、QFN-24、24-pin QFN 统一成同一个比较键。"""

    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(
        r"(?<![A-Za-z0-9])(?:pin|pins|package|pkg)(?![A-Za-z0-9])",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n,;:")
    if not text:
        return ""

    family_match = re.search(
        rf"(?<![A-Za-z0-9])(?P<family>{PACKAGE_FAMILY_PATTERN})"
        r"(?:\s*[- ]?\s*(?P<count>\d{1,4}))?(?![A-Za-z0-9])",
        text,
        flags=re.IGNORECASE,
    )
    count = ""
    if family_match:
        count = family_match.group("count") or ""
        if not count:
            count_match = re.search(
                r"(?<![A-Za-z0-9])(\d{1,4})(?![A-Za-z0-9])",
                text,
            )
            count = count_match.group(1) if count_match else ""
        return normalize_compact(f"{family_match.group('family')}{count}")

    embedded_package = extract_public_package_name_from_metadata(text)
    return normalize_compact(embedded_package or text)


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
