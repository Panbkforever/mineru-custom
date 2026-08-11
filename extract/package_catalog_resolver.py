"""建立文档级物理封装槽位，并把已确认的引脚表绑定到槽位。

本模块位于“表格/字段判断”之后、“逐行生成引脚记录”之前。它只处理
最终 JSON 最外层的 ``pkg``，不判断 pin_no、pin_name、type，也不修改
任何表格行内容。

固定处理流程：

1. 从全文表格中定位可能的封装总述表或包装信息表。当前表自己的表题命中
   器件信息、封装信息、订购信息及对应英文时属于最高优先级；否则只从
   目录前、目录结束后三页或文档末十页等限定页面区域召回。章节标题不能
   代替当前表题触发最高优先级。
2. 第二次模型只判断总述表结构、表头行和 device/pkg 相关列角色；代码随后
   从原始单元格完整读取 device-pkg 关系，不改写源值。
3. 第三次模型每篇 PDF 只调用一次。输入完整 device-pkg 关系，以及第一次
   已确认引脚表的完整表题和表头；输出只包含类别数量、完整 pkg 名称和组成
   该类别的关系编号，不返回任何表格绑定。
4. 代码校验第三次返回：pkg 必须逐字来自第二阶段关系，关系编号不能越界或
   跨类别重复。校验通过后冻结为 slot:0、slot:1……。
5. 第三次类别调用失败或没有形成有效类别时，才使用既有物理元数据和严格
   表内分支作为兼容兜底，不能让临时模型错误直接清空整篇结果。
6. 严格确认的 N 个表内分支是 N 个独立输出槽位的下限。总述表即使只找到
   一个公开封装名，也必须建立 N 个槽位并复用该名称；最终输出再追加数字
   后缀。没有多分支证据时，包装信息表或单封装兜底才决定槽位数量。
7. 目标引脚表只通过表题、章节标题、表头和多封装列标签绑定已有槽位；
   description 和数据行不能参与绑定。
8. 槽位冻结后，任何未匹配表都不能创建新 pkg。单封装文档可以绑定唯一
   槽位；多封装文档中无法唯一归属的表必须标记为 unresolved，禁止默认
   塞入第一个槽位。真实名称缺失时仅对已经确认的槽位使用 a、b、c……。
9. 多封装表的全部本地分支必须一次性执行一对一绑定；禁止每个分支独立
   兜底后落入同一个 package_key。标签脚注、drawing 和 pin_count 只用于
   内部消歧，不改变任何引脚行内容。
10. ``XXX Mode Pin Name`` 形成的运行模式分支只控制名称列读取，不创建 pkg
   槽位；这些分支必须共同绑定当前表所属的同一个物理封装。

特别重要的边界：

* 这里不会再次调用引脚表字段判断，也不会生成引脚记录。
* ``multi_package_extractor.py`` 仍只负责单张表内部的多封装结构。
* description 和普通正文不能参与封装绑定。
* 一个 pkg 只能是一个字符串，禁止使用 ``|`` 拼接多个候选名称。
* 第三阶段公开 pkg 保留源关系中的完整封装名，包括 drawing/code 和 pin 数；
  器件型号、订购型号不能写入公开 pkg。
* 不能为每张未匹配表生成 ``unresolved:table_id``，否则表数会被误当成封装数。
* 两个槽位即使公开封装名相同也保持独立；只有建立槽位时的同一行/同一结构
  证据才能决定它们是不是同一个物理映射空间。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence


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

    ``identity_name`` 是器件型号，只参与表格关联；``display_name`` 是第三
    阶段确认的完整公开封装名。旧兼容路径没有该值时才使用
    ``package_type``。
    """

    package_key: str
    # 第三阶段确认的完整公开名称。内部 family/drawing/pin_count 字段继续
    # 独立保留，供现有绑定逻辑匹配表题和表头。
    display_name: str = ""
    identity_name: str = ""
    identity_aliases: list[str] = field(default_factory=list)
    package_type: str = ""
    package_drawing: str = ""
    pin_count: str = ""
    evidence_table_ids: list[int] = field(default_factory=list)


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
PackageCategoryClassifier = Callable[..., Mapping[str, Any]]


def resolve_document_package_catalog(
    *,
    all_tables: Sequence[PackageCatalogTable],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    source_name: str = "",
    use_semantic_classifier: bool = False,
    classifier: PackageCatalogClassifier | None = None,
    category_classifier: PackageCategoryClassifier | None = None,
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
    categories_resolved = False
    if use_semantic_classifier or classifier is not None:
        entries, semantic_diagnostics = classify_package_catalog_candidates(
            catalog_candidates,
            source_name=source_name,
            target_tables=target_tables,
            classifier=classifier,
        )
        diagnostics.extend(semantic_diagnostics)

        # 第二阶段已经从原表读取完整 device-pkg 关系。第三阶段只综合这些
        # 关系和已确认引脚表的表题/表头，确定文档类别；不生成表格绑定。
        categorized_entries, category_diagnostics = resolve_package_categories(
            entries,
            target_tables=target_tables,
            source_name=source_name,
            classifier=category_classifier,
        )
        diagnostics.extend(category_diagnostics)
        if categorized_entries:
            entries = categorized_entries
            categories_resolved = True

    # 第三阶段成功后，类别数量已经冻结，旧的身份敏感去重不能再次改动。
    # 只有第三阶段没有有效结果时，才执行原有兼容去重和表内分支补位。
    # 同一物理封装有时会被总述表和 Packaging Information 分别写成
    # ``(TSSOP-14) - PW`` 与 ``TSSOP / PW / 14``。冻结槽位前只合并物理
    # 元数据完全相同且身份不冲突的重复证据，不能合并不同器件身份。
    if not categories_resolved:
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

    # 总述表没有建立槽位时，严格确认的表内多封装结构可以提供槽位数量。
    # 如果仍无多封装证据，但存在目标引脚表，则整篇文档只建立一个槽位；
    # 绝不能按目标表数量建立槽位。
    if not categories_resolved:
        entries = merge_plan_package_labels(
            entries,
            target_tables=target_tables,
            multi_package_plans=multi_package_plans,
        )
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


def resolve_package_categories(
    entries: Sequence[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    source_name: str,
    classifier: PackageCategoryClassifier | None = None,
) -> tuple[list[PackageCatalogEntry], list[dict[str, Any]]]:
    """综合总述关系和目标表表题/表头，确定文档级 pkg 类别。

    本函数是第三次模型调用的唯一入口。它不调用 ``bind_target_tables``，也
    不产生任何 ``PackageAssignment``。模型只负责把源关系划分类别；完整
    pkg 名称、器件身份和物理元数据都由代码从第二阶段结果中回填。
    """

    relations = build_device_package_relations(entries)
    if not relations or not target_tables:
        return [], [
            {
                "stage": "package_category",
                "status": "skipped",
                "reason": (
                    "no_device_package_relations"
                    if not relations
                    else "no_confirmed_pin_tables"
                ),
            }
        ]

    try:
        if classifier is None:
            from extract.semantic_classifier import (
                classify_document_package_categories,
            )

            response = classify_document_package_categories(
                [public_category_relation(relation) for relation in relations],
                target_tables,
                source_name=source_name,
            )
        else:
            response = classifier(
                [public_category_relation(relation) for relation in relations],
                target_tables,
                source_name,
            )
    except Exception as exc:
        # 类别调用是增强阶段。瞬时 API 错误时保留第二阶段关系进入既有兼容
        # 路径，不能因为一次额外请求失败而清空整个 PDF。
        return [], [
            {
                "stage": "package_category",
                "status": "error",
                "reason": str(exc),
                "relation_count": len(relations),
            }
        ]

    categorized = build_entries_from_category_response(
        entries,
        relations,
        response,
    )
    diagnostics = [
        {
            "stage": "package_category",
            "status": "accepted" if categorized else "invalid_or_empty",
            "relation_count": len(relations),
            "target_table_count": len(target_tables),
            "category_count": len(categorized),
            "categories": [
                {
                    "pkg": entry.display_name,
                    "identity_name": entry.identity_name,
                    "identity_aliases": list(entry.identity_aliases),
                    "package_type": entry.package_type,
                    "package_drawing": entry.package_drawing,
                    "pin_count": entry.pin_count,
                }
                for entry in categorized
            ],
        }
    ]
    return categorized, diagnostics


def build_device_package_relations(
    entries: Sequence[PackageCatalogEntry],
) -> list[dict[str, Any]]:
    """把第二阶段目录条目展开成完整 device-pkg 关系。

    同一目录项中的身份别名分别形成关系，但都指回同一个源条目。匿名包装
    元数据也保留为空 device 的关系，使只有 Packaging Information 的文档
    仍可参与类别判断。
    """

    relations: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(entries):
        pkg = complete_package_name(entry)
        if not pkg:
            continue
        devices = [entry.identity_name, *entry.identity_aliases]
        devices = [device for device in devices if device] or [""]
        seen_devices: set[str] = set()
        for device_index, device in enumerate(devices):
            normalized_device = normalize_compact(device)
            if normalized_device in seen_devices:
                continue
            seen_devices.add(normalized_device)
            relations.append(
                {
                    "relation_id": f"rel:{entry_index}:{device_index}",
                    "device": device,
                    "pkg": pkg,
                    "entry_index": entry_index,
                }
            )
    return relations


def public_category_relation(relation: Mapping[str, Any]) -> dict[str, str]:
    """移除内部 entry_index，只把项目约定的三项发送给类别模型。"""

    return {
        "relation_id": str(relation.get("relation_id", "")),
        "device": str(relation.get("device", "")),
        "pkg": str(relation.get("pkg", "")),
    }


def complete_package_name(entry: PackageCatalogEntry) -> str:
    """组合完整 pkg 展示名，保留 drawing/code 和 pin/ball 数量。"""

    package_type = clean_metadata(entry.package_type)
    drawing = clean_metadata(entry.package_drawing)
    pin_count = clean_pin_count(entry.pin_count)
    if not package_type:
        package_type = drawing
        drawing = ""
    if not package_type:
        return ""

    parts = [package_type]
    if drawing and normalize_compact(drawing) not in normalize_compact(package_type):
        parts.append(f"({drawing})")
    result = " ".join(parts)
    if pin_count and not re.search(
        rf"(?<!\d){re.escape(pin_count)}\s*[- ]?\s*(?:pin|ball)s?(?![a-z])",
        result,
        flags=re.IGNORECASE,
    ):
        result = f"{result} {pin_count}-pin"
    return re.sub(r"\s+", " ", result).strip()


def build_entries_from_category_response(
    source_entries: Sequence[PackageCatalogEntry],
    relations: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
) -> list[PackageCatalogEntry]:
    """把类别模型的关系分组转换回现有目录条目。

    pkg 必须逐字等于当前类别某条关系的 pkg；关系不能跨类别重复。模型只
    能分组，不能修改公开名称或内部匹配元数据。
    """

    relation_by_id = {
        str(relation.get("relation_id", "")): relation
        for relation in relations
    }
    used_relation_ids: set[str] = set()
    result: list[PackageCatalogEntry] = []
    invalid_response = False
    for item in response.get("categories") or []:
        if not isinstance(item, Mapping):
            invalid_response = True
            continue
        relation_ids = []
        for raw_relation_id in item.get("relation_ids") or []:
            relation_id = str(raw_relation_id)
            if (
                relation_id not in relation_by_id
                or relation_id in used_relation_ids
                or relation_id in relation_ids
            ):
                invalid_response = True
                continue
            relation_ids.append(relation_id)
        if not relation_ids:
            invalid_response = True
            continue

        pkg = str(item.get("pkg") or "")
        valid_pkg_values = {
            str(relation_by_id[relation_id].get("pkg") or "")
            for relation_id in relation_ids
        }
        if not pkg or pkg not in valid_pkg_values:
            invalid_response = True
            continue

        entry_indexes = []
        for relation_id in relation_ids:
            entry_index = int(relation_by_id[relation_id]["entry_index"])
            if entry_index not in entry_indexes:
                entry_indexes.append(entry_index)
        members = [source_entries[index] for index in entry_indexes]
        representative = next(
            (
                member
                for member in members
                if complete_package_name(member) == pkg
            ),
            members[0],
        )
        identities: list[str] = []
        evidence_table_ids: list[int] = []
        for member in members:
            for identity in [member.identity_name, *member.identity_aliases]:
                if identity and identity not in identities:
                    identities.append(identity)
            for table_id in member.evidence_table_ids:
                if table_id not in evidence_table_ids:
                    evidence_table_ids.append(table_id)

        result.append(
            PackageCatalogEntry(
                package_key="",
                display_name=pkg,
                identity_name=identities[0] if identities else "",
                identity_aliases=identities[1:],
                package_type=representative.package_type,
                package_drawing=representative.package_drawing,
                pin_count=representative.pin_count,
                evidence_table_ids=evidence_table_ids,
            )
        )
        used_relation_ids.update(relation_ids)

    # 第三阶段负责确定整篇文档的类别，不能只返回一部分“容易判断”的关系。
    # 不完整结果整体作废，由调用方进入旧兼容路径，避免类别数量静默减少。
    if invalid_response or used_relation_ids != set(relation_by_id):
        return []
    return result


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
    return PackageCatalogDecision(
        is_package_summary=(
            bool(response.get("is_package_summary")) and structurally_valid
        ),
        table_role=table_role if structurally_valid else "irrelevant",
        header_row_index=header_row_index,
        columns=tuple(columns),
    )


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


def merge_plan_package_labels(
    entries: list[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
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


def bind_target_tables(
    *,
    entries: Sequence[PackageCatalogEntry],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
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
    """把一张表的全部本地分支一次性绑定到互不重复的文档槽位。

    绑定是一个小规模最大权重匹配问题。分支标签与目录项之间的型号、封装族、
    drawing 和显式 pin_count 共同计分；无证据时才用列顺序打破平局。无论
    得分高低，同一张多封装表内都禁止两个分支复用同一个 ``package_key``。
    """

    if len(entries) < len(local_labels):
        raise ValueError(
            "package catalog has fewer slots than confirmed table branches"
        )

    score_matrix = [
        [
            multi_package_binding_score(label, entry, local_slot, entry_slot)
            for entry_slot, entry in enumerate(entries)
        ]
        for local_slot, label in enumerate(local_labels)
    ]
    selected_slots = maximum_weight_unique_assignment(score_matrix)
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
    local_slot: int,
    entry_slot: int,
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
        0,
    )
    return PackageAssignment(
        package_key=entry.package_key,
        pkg=(
            clean_metadata(entry.display_name)
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


def clean_package_name(value: str) -> str:
    """兼容旧调用名称；当前语义等同于清理公开物理封装名。"""

    return clean_public_package_name(value)


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
