"""建立文档级封装目录，并把已确认的引脚表绑定到真实封装。

本模块位于“表格/字段判断”之后、“逐行生成引脚记录”之前。它只处理
最终 JSON 最外层的 ``pkg``，不判断 pin_no、pin_name、type，也不修改
任何表格行内容。

固定处理流程：

1. 从全文表格中宽松定位可能的封装总述表或包装信息表。
2. 模型只判断表格角色、表头行和列角色，不返回任何 pkg 值。
3. 代码按照模型给出的列索引逐行读取原表。只有 ``identity_summary`` 表
   的 ``package_identity`` 列可以创建 pkg。
4. ``packaging_metadata`` 表只能通过 ``orderable_sku`` 给已有 pkg 补充
   package_type、package_drawing 和 pin_count，不能创建新 pkg。
5. 目标引脚表只通过表题、章节标题、表头和多封装列标签绑定目录项；
   description 和数据行不能参与绑定。
6. 最终返回稳定的内部 package_key 和单个真实 pkg 名称。内部 key 只用于
   分组，绝不写入公开 JSON。

特别重要的边界：

* 这里不会再次调用引脚表字段判断，也不会生成引脚记录。
* ``multi_package_extractor.py`` 仍只负责单张表内部的多封装结构。
* description 和普通正文不能参与封装绑定。
* 一个 pkg 只能是一个字符串，禁止使用 ``|`` 拼接多个候选名称。
* pkg 名称最长 15 个字符；超过长度的标题、描述或多个名称拼接结果直接拒绝。
* 无法确定真实名称时输出空字符串；不能退回旧的 a/b/c 假名称。
* 订购型号、封装类型和封装 Drawing 都不能单独创建 pkg。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    """一个由总述表原始单元格创建的真实封装目录项。"""

    package_key: str
    name: str
    aliases: list[str] = field(default_factory=list)
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

    def assignment_for(self, table_id: int, local_slot: int) -> PackageAssignment:
        """读取一个表内槽位的绑定；缺失时返回独立的未解析分组。"""

        assignment = self.assignments.get((table_id, local_slot))
        if assignment is not None:
            return assignment
        return PackageAssignment(
            package_key=f"unresolved:{table_id}:{local_slot}",
            pkg="",
            reason="package_assignment_missing",
        )


PackageCatalogClassifier = Callable[..., Mapping[str, Any]]


def resolve_document_package_catalog(
    *,
    all_tables: Sequence[PackageCatalogTable],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    source_name: str = "",
    use_semantic_classifier: bool = False,
    classifier: PackageCatalogClassifier | None = None,
) -> PackageCatalogResolution:
    """建立封装目录并为每张目标表生成唯一绑定结果。

    ``all_tables`` 包含全文表格，因此订购表、Device Information 等即使不属于
    引脚表，也仍可以成为封装总述候选。``target_tables`` 只包含已经确认要
    提取的物理引脚表，两者职责不能混用。
    """

    diagnostics: list[dict[str, Any]] = []
    entries: list[PackageCatalogEntry] = []

    catalog_candidates = find_package_catalog_candidates(all_tables)
    if use_semantic_classifier or classifier is not None:
        entries, semantic_diagnostics = classify_package_catalog_candidates(
            catalog_candidates,
            source_name=source_name,
            target_tables=target_tables,
            classifier=classifier,
        )
        diagnostics.extend(semantic_diagnostics)

    # 只有整篇文档没有找到任何身份总述表时，才允许表内多封装标签作为
    # 兜底目录。目录一旦存在，package type/drawing 标签只能参与绑定，
    # 不能再创建与真实器件身份并列的新 pkg。
    entries = merge_plan_package_labels(
        entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
    )

    assignments = bind_target_tables(
        entries=entries,
        target_tables=target_tables,
        multi_package_plans=multi_package_plans,
        diagnostics=diagnostics,
    )
    return PackageCatalogResolution(entries, assignments, diagnostics)


def find_package_catalog_candidates(
    tables: Sequence[PackageCatalogTable],
) -> list[PackageCatalogTable]:
    """定位可能记录封装总数和名称的总述表。

    这是宽松召回，不是最终判断。表题/章节、表头以及文档前后区域共同计分，
    防止把规则写死为某一种 ``Device Information`` 或 ``Ordering`` 表头。
    """

    if not tables:
        return []
    known_pages = [
        table.page_idx
        for table in tables
        if isinstance(table.page_idx, int) and table.page_idx >= 0
    ]
    last_page = max(known_pages, default=0)
    edge_span = max(2, int((last_page + 1) * 0.15))
    # 最终 Markdown 通常没有页码。此时用全文表格顺序的前后 15% 作为区域
    # 兜底；它只提供一分区域证据，不能让普通表单独成为总述候选。
    table_edge_span = max(2, int(len(tables) * 0.15))

    scored: list[tuple[int, int, PackageCatalogTable]] = []
    for order, table in enumerate(tables):
        has_page = isinstance(table.page_idx, int) and table.page_idx >= 0
        is_order_edge = (
            order < table_edge_span
            or order >= len(tables) - table_edge_span
        )
        score = package_catalog_candidate_score(
            table,
            last_page=last_page,
            edge_span=edge_span,
            is_order_edge=is_order_edge and not has_page,
        )
        is_edge = is_order_edge if not has_page else (
            (
                table.page_idx < edge_span
                or table.page_idx > last_page - edge_span
            )
        )
        has_table_shape = (
            len(table.rows) >= 1
            and max((len(row) for row in table.rows), default=0) >= 2
        )
        # 文档中间的表需要明确标题/表头证据。前后区域只要具有正常二维
        # 表格结构就宽松送模型，避免总述表因为标题和表头名称完全陌生而
        # 在模型之前被规则漏掉；区域只负责召回，不直接认定 pkg。
        if score >= 3 or (is_edge and has_table_shape):
            scored.append((score, order, table))

    # 优先发送高分表，但同分时保持原文顺序，保证模型证据和调试稳定。
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [table for _, _, table in scored]


def package_catalog_candidate_score(
    table: PackageCatalogTable,
    *,
    last_page: int,
    edge_span: int,
    is_order_edge: bool = False,
) -> int:
    """计算总述表召回分数；分数只用于减少模型请求数量。"""

    title_text = normalize_text(
        "\n".join(
            [
                table.title,
                table.group_context,
                *table.current_chapter_titles,
            ]
        )
    )
    header_text = normalize_text(" ".join(table.headers))
    score = 0

    title_terms = (
        "device information",
        "device comparison",
        "device option",
        "product variant",
        "selection guide",
        "ordering information",
        "orderable",
        "package option",
        "package information",
        "packaging information",
        "package type",
        "封装",
        "订购",
        "器件信息",
    )
    header_terms = (
        "package",
        "package type",
        "package drawing",
        "orderable device",
        "part number",
        "model",
        "product",
        "ordering code",
        "marking",
        "device",
        "pins",
        "pin count",
        "body size",
        "封装",
        "器件型号",
        "引脚数",
    )
    score += 3 if any(term in title_text for term in title_terms) else 0
    score += min(4, sum(term in header_text for term in header_terms))

    # 总述表通常位于文档前部或订购/包装章节所在的后部。区域只能加分，
    # 不能单独使一张普通表成为候选。
    if isinstance(table.page_idx, int) and table.page_idx >= 0:
        if table.page_idx < edge_span or table.page_idx > last_page - edge_span:
            score += 1
    elif is_order_edge:
        score += 1
    return score


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
    if classifier is None:
        from extract.semantic_classifier import classify_package_catalog_table

        classifier = classify_package_catalog_table

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
    print(
        f"封装目录判断: 候选总述表 {len(tables)} 张, "
        f"并发 {min(workers, len(tables))}",
        flush=True,
    )

    responses: list[tuple[int, PackageCatalogTable, Mapping[str, Any]]] = []
    diagnostics: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(tables))) as executor:
        futures = {
            executor.submit(
                classifier,
                table,
                source_name,
                target_tables,
            ): (order, table)
            for order, table in enumerate(tables)
        }
        for completed, future in enumerate(as_completed(futures), 1):
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
            print(
                f"封装目录判断进度: {completed}/{len(tables)}",
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
    entries = build_catalog_entries_from_decisions(decisions, diagnostics)
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
) -> list[PackageCatalogEntry]:
    """先由身份总述表创建 pkg，再由包装信息表补充元数据。"""

    entries: list[PackageCatalogEntry] = []

    # 第一遍只处理身份总述表。无论包装信息表在文档中位于前部还是后部，
    # 都必须等真实 pkg 目录建立后才能参与补充。
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

    # 第二遍只允许补充已有目录项。即使订购型号、Drawing、Package Type
    # 看起来像短名称，也绝不能在这一遍创建 pkg。
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
    return entries


def create_identity_entries_from_table(
    entries: list[PackageCatalogEntry],
    table: PackageCatalogTable,
    decision: PackageCatalogDecision,
) -> list[str]:
    """从身份总述表逐行创建 pkg；模型不接触实际输出名称。"""

    created_names: list[str] = []
    identity_columns = columns_for_role(decision, "package_identity")
    for row in table.rows[decision.header_row_index + 1 :]:
        for identity_column in identity_columns:
            name = clean_package_name(cell_value(row, identity_column))
            if (
                not name
                or is_generic_package_label(name)
                or is_repeated_header_value(name, identity_column.header)
            ):
                continue
            package_type = first_role_value(row, decision, "package_type")
            pin_count = clean_pin_count(
                first_role_value(row, decision, "pin_count")
            )
            package_type, type_pin_count = split_package_type_and_pin_count(
                package_type
            )
            incoming = PackageCatalogEntry(
                package_key=make_package_key(name),
                name=name,
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
            if name not in created_names:
                created_names.append(name)
    return created_names


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
        if entry.name not in enriched_names:
            enriched_names.append(entry.name)
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
        identity = normalize_compact(entry.name)
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
    unique = {entry.package_key: entry for entry in best_entries}
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
    """只按规范名称或明确别名合并，不按封装家族和相似度猜测。"""

    incoming_names = {
        normalize_compact(incoming.name),
        *(normalize_compact(alias) for alias in incoming.aliases),
    }
    for existing in entries:
        existing_names = {
            normalize_compact(existing.name),
            *(normalize_compact(alias) for alias in existing.aliases),
        }
        if incoming_names.isdisjoint(existing_names):
            continue
        for alias in [incoming.name, *incoming.aliases]:
            if alias != existing.name and alias not in existing.aliases:
                existing.aliases.append(alias)
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


def merge_plan_package_labels(
    entries: list[PackageCatalogEntry],
    *,
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
) -> list[PackageCatalogEntry]:
    """目录为空时，才用表内多封装标签建立兜底目录。

    身份总述表已经创建真实 pkg 后，表内 ``QFN``、``VSSOP``、``DGK`` 等
    标签只能用于后面的绑定，不能再创建新的 pkg。
    """

    result = list(entries)
    if result:
        return result
    target_ids = {table.table_id for table in target_tables}
    for table_id, plan in multi_package_plans.items():
        if table_id not in target_ids or not plan.is_multi_package:
            continue
        for binding in plan.bindings:
            name = clean_package_name(binding.package)
            if not name or is_generic_package_label(name):
                continue
            merge_catalog_entry(
                result,
                PackageCatalogEntry(
                    package_key=make_package_key(name),
                    name=name,
                    evidence_table_ids=[table_id],
                ),
            )
    return result


def bind_target_tables(
    *,
    entries: Sequence[PackageCatalogEntry],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    diagnostics: list[dict[str, Any]],
) -> dict[tuple[int, int], PackageAssignment]:
    """把每张目标表的本地槽位绑定到一个且仅一个真实 pkg。"""

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

        table_text = binding_evidence_text(table)
        explicit_matches = match_entries_in_text(entries, table_text)

        for local_slot, local_label in enumerate(local_labels):
            local_matches = match_entries_in_text(entries, local_label)
            matches = local_matches or explicit_matches
            reason = ""

            if len(matches) == 1:
                entry = matches[0]
                reason = (
                    "multi_package_binding_label"
                    if local_matches
                    else "table_title_or_header"
                )
                assignment = PackageAssignment(
                    entry.package_key,
                    entry.name,
                    reason,
                )
            elif len(entries) == 1:
                entry = entries[0]
                assignment = PackageAssignment(
                    entry.package_key,
                    entry.name,
                    "single_document_package",
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
                assignment = PackageAssignment(
                    package_key=f"unresolved:{table.table_id}:{local_slot}",
                    pkg="",
                    reason=(
                        "ambiguous_package_evidence"
                        if len(matches) > 1
                        else "no_package_evidence"
                    ),
                )

            assignments[(table.table_id, local_slot)] = assignment
            diagnostics.append(
                {
                    "stage": "package_binding",
                    "table_id": table.table_id,
                    "local_slot": local_slot,
                    "local_label": local_label,
                    "pkg": assignment.pkg,
                    "reason": assignment.reason,
                }
            )

        # 只有单一且基于当前表明确文字命中的结果，才允许成为后续续表来源。
        first_assignment = assignments[(table.table_id, 0)]
        if first_assignment.reason in {
            "multi_package_binding_label",
            "table_title_or_header",
        }:
            previous_explicit = first_assignment
            previous_context = chapter_context_key(table)
        elif chapter_context_key(table) != previous_context:
            previous_explicit = None
            previous_context = ""
    return assignments


def match_entries_in_text(
    entries: Sequence[PackageCatalogEntry],
    text: str,
) -> list[PackageCatalogEntry]:
    """按完整名称边界匹配目录，不执行编辑距离或前缀猜测。"""

    normalized_text = normalize_text(text)
    matches = []
    for entry in entries:
        # 真实名称和显式别名优先；封装类型和 Drawing 只能用于绑定，不会
        # 创建目录项。若同一种元数据对应多个 pkg，会保持歧义而不武断选择。
        names = [entry.name, *entry.aliases]
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


def binding_evidence_text(table: PackageTargetTable) -> str:
    """只拼接允许参与绑定的局部元数据，明确排除数据行和 description。"""

    return "\n".join(
        [
            table.title,
            *table.current_chapter_titles,
            *table.headers,
        ]
    )


def chapter_context_key(table: PackageTargetTable) -> str:
    """生成续表继承使用的当前章节键。"""

    return normalize_text("\n".join(table.current_chapter_titles))


def clean_package_name(value: str) -> str:
    """清理原始身份单元格；一个名称必须保持为单个短字符串。"""

    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,;:")
    # 多封装编号列表头常写作 ``SSOP 28 Pin``。末尾 Pin 只是列角色，
    # 不属于 pkg 名称；只删除末尾完整单词，不能改动名称内部字符。
    value = re.sub(r"\s+\bpin\b\s*$", "", value, flags=re.IGNORECASE)
    if not value or "|" in value or "\n" in value or len(value) > 15:
        return ""
    return value


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


def make_package_key(name: str) -> str:
    """从规范名称生成仅供内部字典使用的稳定 key。"""

    return f"pkg:{normalize_compact(name)}"


def normalize_text(value: str) -> str:
    """保留词边界的通用比较文本。"""

    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = value.lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_compact(value: str) -> str:
    """去除分隔符，用于核对名称是否真实出现在证据文本中。"""

    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())
