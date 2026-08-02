"""从 MinerU 的表格结果中提取器件引脚和封装信息。

本文件只做一件事：把表格转换为项目约定的 JSON 结构。

处理流程固定为八个阶段，阶段之间不互相调用：

1. 表格判断：判断当前表是不是“物理引脚/封装关系表”。
2. 表头结构：仅对候选表展开 rowspan/colspan，建立每列的完整表头路径。
3. 字段判断：只判断每一列的语义，不读取行来生成输出记录。
4. 字段校验：按表头结构恢复被模型漏掉的分支列，或固定选择一个等价名称列。
5. 多封装分析：只按表格结构生成多个编号列/行的绑定计划。
6. 封装槽位：先冻结物理封装数量，再绑定每张引脚表；器件型号只用于关联。
7. 行提取：单封装和多封装走各自独立逻辑，完整读取已经绑定的数据行。
8. 结果整理：按固定槽位分组；pkg 使用物理封装名，未知时按 a/b/c 回退。

特别重要的项目规则：

* 一列一旦被判定为需要字段，就不因为某一行为空而跳过该行。
* 物理引脚表必须存在 pin_no 列映射；模型即使返回 ``should_extract=true``，
  缺少 pin_no 映射时仍由代码明确拒绝，不能进入行提取后静默得到空结果。
* pin_name 不是表级必需字段。整张表没有 pin_name 列或某行 pin_name 为空时，
  该行仍保留，并在最终清洗阶段统一填充为 ``Reserved``。
* 已确定存在 pin_no 列后，某个数据行的 pin_no 单元格为空也保留为
  ``pin_no: ""``；完全空白的结构占位行不属于数据行，仍然跳过。
* pin_no 按原文中的空格、逗号、斜杠等显式分隔符拆分。
* 对同一字母前缀且数字递增的范围进行展开，例如 A1-A5 展开为 A1、A2、A3、A4、A5；
  前后字母不同的 A1-C3 不展开，数字倒序的 A5-A1 也不展开。
* 对 BGA 行列编号的方括号范围进行展开，例如 L[7:12] 展开为 L7 至 L12；
  此处输入已经由字段判断确定为 pin_no，因此不限制字母前缀长度。
* pin_no 和 pin_name 的多个值按位置对应；只有两列拆分后的数量完全一致时才同步拆分。
  数量不一致时保留原 pin_name，不强行猜测对应关系。
* HTML 单元格中的 ``<br>`` 在表格读取阶段必须保留。只有 pin_no 和 pin_name
  能按 ``<br>`` 直接一一对应，或相同分组内的数字范围展开后数量完全一致时，
  才按位置配对；例如 ``LED[4:0]_3`` 可按降序展开。表头、描述和普通空格
  不按此规则拆分，任一分组数量不一致时保留原 pin_name。
* 当前只对 pin_no 和 pin_name 做跨值同步拆分，不拆 type、description 等其他字段。
* 表格存在 DESCRIPTION 列时，每条输出记录增加 ``description``，取同一原始
  数据行中的完整描述；一行拆成多个引脚时共享该描述，没有 DESCRIPTION 列
  的表不输出该字段。DESCRIPTION 由代码按表头补充，不扩大模型职责。
* DESCRIPTION 列是只读附加字段，其表头和单元格内容都不能参与 pin、type
  或多封装结构判断。即使描述中出现 package、pin、ball 等词，也不能
  改变表头边界或触发多封装分支。
* pin_name 为空填 ``Reserved``；去掉末尾的 ``(数字)`` 和 ``(continued)``。
* 同一个 pin_no 出现多次时不合并记录；不同 type 也不合并。
* 多个 type 列同时存在时，只保留最接近 signal/pin 语义的一个，优先 SIGNAL TYPE、PIN TYPE、I/O TYPE。
* BALL NAME、SIGNAL NAME、PIN NAME、TERMINAL NAME 在项目中属于等价
  的 pin_name 语义。普通表同时出现多个等价名称列时，只固定选择完整度最高
  的一列，同分时选择最左列，禁止使用 ``|`` 合并多个名称。
* 多个名称列只有在多层表头中共享同一个名称父节点，并具有不同的子分支标签
  时，才属于“一个共享 pin_no + 多个分支 pin_name”的多封装结构。此时每个
  分支必须保留一个名称列，模型漏选的分支由确定性表头结构恢复。
* “多个封装各有 pin_no、共享一个 pin_name”和“共享一个 pin_no、多个封装
  各有 pin_name”是两条独立多封装分支，不能通过合并同名字段相互转换。
* “Pin Configuration and Function” 这类坐标矩阵不是物理引脚表，表级直接排除。
* 开启语义判断时，模型接收初筛后的表格标题、表头和完整表格；模型只返回
  ``should_extract`` 以及 ``pin_no``、``pin_name``、``type`` 的列映射。
* 表格分组标题在“上一张表结束到当前表开始”的局部文本窗口中识别：
  优先使用 ``Table xxx``/``表 xxx`` 明确表题；没有表号时允许使用紧邻
  表格的独立短标题，例如 ``Pin Functions``，不要求包含 Pin、Signal 等
  固定关键词。局部窗口进入新章节时禁止继承上一章节的旧表题；没有新章节
  和新标题时才继承上一表题，用于无重复标题的跨页续表。
* 最终 JSON 的 group 只使用当前表格表题；``(continued)`` 清理后相同的
  原表和续表归入同一 group。上一章和当前章标题仍作为内部上下文保留，
  只供模型判断和封装绑定使用，不能写入最终 group。
* 表内的 Power Pins、PCI INTERFACE 等结构标题行只负责划分原表内容；
  行提取时跳过这些标题行，但不得追加或覆盖最终 group。
* 初筛后先调用 ``special_table_handlers.py``。特殊表只有完整命中专用规则才
  绕过模型；Reserved/NC 表直接保留真实 Reserved 行并排除不存在位置；
  Word 位字段表中的 ``PIN AFFECTED`` 只是寄存器辅助引用列，严格命中专用
  规则后整表过滤，不能进入模型或行提取。
* 横向重复的 ``Pin# | Pin Name | Type`` 字段块必须至少完整重复两次且字段
  顺序完全一致才命中；命中后整张表直接排除，不送模型、不进入行提取。
* 通过表格/字段判断后调用 ``multi_package_extractor.py``。多个封装专属
  pin_no 列、package 控制列和 package 分段行走多封装分支；其中的 package
  文字是表内结构证据，但最终名称仍由文档级目录统一校验。
* ``package_catalog_resolver.py`` 是唯一的真实 pkg 判断模块。它先从全文表格
  定位封装总述候选，再结合已经确认的多封装结构和当前表题/表头完成绑定。
* 封装目录判断不能修改表格是否提取、字段映射、行内容或 group；逐行提取
  不能反过来创造、合并或重命名 pkg。
* 粗解析找不到表头时，如果原 HTML 同时具有 rowspan/colspan 和明确的
  PIN/BALL/引脚结构锚点，必须先执行 span-aware 表头重试；不能在展开
  多层表头之前直接以 ``no_candidate_header`` 丢弃整张表。
* 封装槽位数量在行提取前冻结；未匹配表不能按 table_id 创建新的外层 pkg。
* 已严格确认的 N 个表内分支是 N 个独立封装槽位的下限。封装目录即使只
  识别到一个真实 pkg 名称，也只能把该名称复用给 N 个独立槽位，不能把
  分支数据合并到同一个 package_key；最终名称由统一后缀规则区分。
* 器件型号只参与槽位关联，不能写入 pkg。pkg 只使用物理封装类型；名称
  不明确时按固定槽位顺序使用 a、b、c……，但槽位数量保持不变。
* 一个 pkg 只能是一个名称字符串，禁止使用 ``|`` 拼接多个名称；真实名称
  最长 15 个字符。
* 最终 JSON 不允许出现完全相同的 pkg 名称。同名 pkg 出现多次时，按冻结
  槽位顺序从第一个开始追加 1、2、3……；只出现一次的 pkg 名称保持不变。
* 语义字段判断默认并发数为 4，可通过 ``EXTRACT_SCHEMA_WORKERS`` 覆盖。
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from extract.special_table_handlers import find_special_table_match
from extract.parallel_cell_splitter import (
    parse_html_cell_text,
    split_parallel_pin_names as split_parallel_pin_names_by_structure,
)
from extract.repeated_horizontal_table_filter import (
    is_repeated_horizontal_pin_block_table,
)
from extract.multi_package_extractor import (
    BoundPackageRow,
    MultiPackagePlan,
    analyze_multi_package_table,
    iter_bound_package_rows,
    plan_to_debug,
)
from extract.group_title_context import (
    GroupTitleContextTracker,
    extract_numbered_table_title,
    join_group_titles,
    resolve_table_title,
)
from extract.package_catalog_resolver import (
    PackageAssignment,
    PackageCatalogTable,
    PackageTargetTable,
    catalog_header_hints,
    resolve_document_package_catalog,
)
from extract.table_header_structure import (
    NameColumnBranch,
    NameColumnLayout,
    analyze_name_column_layout,
    build_header_paths,
    header_paths_to_lists,
    name_layout_to_dict,
    parse_spanned_table,
    resolve_header_boundary,
)


# ---------------------------------------------------------------------------
# 数据结构：判断阶段的结果和输出阶段的结果分开
# ---------------------------------------------------------------------------


@dataclass
class TableCandidate:
    html: str
    page_idx: int | None
    title: str = ""
    # group_context 与 title 必须分开：前者包含跨章节上下文，后者只保存
    # 当前表题并继续用于模型、封装关联和多封装结构分析。
    group_context: str = ""
    # 两组标题分别保存，封装判断只能读取当前章；上一章仍只用于 group。
    previous_chapter_titles: tuple[str, ...] = ()
    current_chapter_titles: tuple[str, ...] = ()


@dataclass
class ColumnDecision:
    index: int
    raw_header: str
    field_name: str
    score: int = 0


@dataclass
class TableDecision:
    """表级判断结果，不包含任何已经提取出的 pin 记录。"""

    should_extract: bool
    table_role: str = ""
    group: str = ""
    reason: str = ""
    confidence: float = 0.0
    columns: list[ColumnDecision] = field(default_factory=list)
    # None 表示提取全部数据行；特殊表可以只允许确定的行进入统一提取流程。
    included_row_indexes: frozenset[int] | None = None


@dataclass
class ExtractedGroup:
    group: str
    pin_list: list[dict[str, Any]] = field(default_factory=list)


LAST_EXTRACTION_DEBUG: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# 对外入口：所有入口都使用同一条流水线
# ---------------------------------------------------------------------------


def extract_pin_package_info_from_middle_json(
    middle_json: dict[str, Any],
    source_name: str = "",
    include_debug: bool = False,
    use_semantic_classifier: bool = False,
) -> list[dict[str, Any]]:
    """从 MinerU 的 middle_json 找出表格，并交给统一提取流水线。"""
    return extract_pin_package_info_from_table_candidates(
        iter_table_candidates(middle_json),
        source_name=source_name,
        include_debug=include_debug,
        use_semantic_classifier=use_semantic_classifier,
    )


def extract_pin_package_info_from_middle_json_file(
    path: str | Path,
    use_semantic_classifier: bool = False,
    include_debug: bool = False,
) -> list[dict[str, Any]]:
    """读取 middle_json 文件后调用内存对象入口。"""
    path = Path(path)
    return extract_pin_package_info_from_middle_json(
        json.loads(path.read_text(encoding="utf-8")),
        source_name=path.stem,
        use_semantic_classifier=use_semantic_classifier,
        include_debug=include_debug,
    )


def extract_pin_package_info_from_markdown_json_file(
    path: str | Path,
    use_semantic_classifier: bool = False,
    include_debug: bool = False,
) -> list[dict[str, Any]]:
    """从最终的 ``<pdf>.json`` 中提取，确保使用后处理后的表格。"""

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    markdown = payload.get("markdown", "") if isinstance(payload, dict) else ""
    if not isinstance(markdown, str) or "<table" not in markdown.lower():
        return []
    return extract_pin_package_info_from_table_candidates(
        iter_table_candidates_from_markdown(markdown),
        source_name=path.stem,
        include_debug=include_debug,
        use_semantic_classifier=use_semantic_classifier,
    )


def extract_pin_package_info_from_table_candidates(
    tables: Iterable[TableCandidate],
    source_name: str = "",
    include_debug: bool = False,
    use_semantic_classifier: bool = False,
) -> list[dict[str, Any]]:
    """按“先判断、后提取”的顺序处理表格。

    ``prepared`` 保存候选表和字段判断所需的信息；只有
    ``TableDecision.should_extract`` 为真时，才会调用行提取函数。
    """

    global LAST_EXTRACTION_DEBUG
    LAST_EXTRACTION_DEBUG = []
    # 转成列表，保证封装目录扫描和引脚表判断读取的是同一份有序表格。
    candidates = list(tables)
    packages: dict[str, dict[str, Any]] = {}

    # 第一阶段：只准备候选表，不创建任何 pin 记录。
    prepared = []
    parsed_tables = [
        (table_id, table, parse_html_table(table.html))
        for table_id, table in enumerate(candidates)
    ]
    for table_id, table, rough_rows in parsed_tables:
        # rough_rows 只用于低成本初筛。通过初筛后会重新按 rowspan/colspan
        # 展开候选表，字段判断和最终提取不能继续使用错位的扁平数组。
        debug = {
            "table_id": table_id,
            "source": source_name,
            "page": table.page_idx + 1 if isinstance(table.page_idx, int) else None,
            "title": table.title,
            "group_context": table.group_context,
            "row_count": len(rough_rows),
            "status": "pending",
            "skip_reason": "",
        }
        LAST_EXTRACTION_DEBUG.append(debug)

        if len(rough_rows) < 2:
            skip(debug, "too_few_rows")
            continue
        if is_pinout_matrix_table(rough_rows, table.title):
            skip(debug, "pinout_matrix_table")
            continue

        # 多层表头可能只有展开 rowspan/colspan 后才能组成“引脚 > 名称”或
        # “PIN > Package A”字段路径。rough_rows 失败时只对具有明确结构
        # 锚点的 HTML 重试，避免把普通无关表无条件送入后续昂贵流程。
        span_rows: list[list[str]] | None = None
        rough_header_index, rough_headers = choose_header_row(
            rough_rows,
            table.title,
            semantic=use_semantic_classifier,
        )
        if rough_header_index < 0:
            if not should_retry_spanned_header(table, rough_rows):
                skip(debug, "no_candidate_header")
                continue
            span_rows = parse_spanned_table(table.html)
            rough_header_index, rough_headers = choose_header_row(
                span_rows,
                table.title,
                semantic=use_semantic_classifier,
            )
            if rough_header_index < 0:
                skip(debug, "no_candidate_header_after_span_retry")
                continue
            debug["header_retry"] = "span_aware"
        rough_source_rows = span_rows or rough_rows
        rough_data_rows = rough_source_rows[rough_header_index + 1 :]
        if is_ordering_table(rough_headers):
            skip(debug, "ordering_table")
            continue
        if not is_loose_candidate(
            table.title,
            rough_headers,
            rough_data_rows,
        ):
            skip(debug, "not_pin_table_candidate")
            continue
        if is_non_physical_port_function_table(table.title, rough_headers):
            skip(debug, "non_physical_port_function_table")
            continue

        # 只有已经通过宽松初筛的表才执行 span-aware 解析。展开后的 rows
        # 是后续字段判断、多封装分析和逐行提取共同使用的唯一二维表。
        rows = span_rows or parse_spanned_table(table.html) or rough_rows
        if len(rows) < 2:
            skip(debug, "too_few_rows_after_span_expansion")
            continue
        if is_pinout_matrix_table(rows, table.title):
            skip(debug, "pinout_matrix_table_after_span_expansion")
            continue

        header_index, headers = choose_header_row(
            rows,
            table.title,
            semantic=use_semantic_classifier,
        )
        if header_index < 0:
            skip(debug, "no_candidate_header_after_span_expansion")
            continue
        header_paths = build_header_paths(rows, header_index)
        headers = [path.combined for path in header_paths]
        name_layout = analyze_name_column_layout(header_paths)
        data_rows = rows[header_index + 1 :]

        # 横向重复字段块是已确认不需要的冗余引脚列表。必须在模型判断和
        # 多封装分析之前整表排除，不能再按普通单封装表进入行提取。
        if is_repeated_horizontal_pin_block_table(headers):
            debug["headers"] = headers
            skip(debug, "repeated_horizontal_pin_blocks")
            continue
        if is_ordering_table(headers):
            skip(debug, "ordering_table_after_span_expansion")
            continue
        if not is_loose_candidate(table.title, headers, data_rows):
            skip(debug, "not_pin_table_candidate_after_span_expansion")
            continue
        if is_non_physical_port_function_table(table.title, headers):
            skip(debug, "non_physical_port_function_table_after_span_expansion")
            continue

        rule_columns = classify_columns(headers, data_rows, table.title)
        prepared.append(
            {
                "table_id": table_id,
                "table": table,
                "rows": rows,
                "header_index": header_index,
                "headers": headers,
                "header_paths": header_paths,
                "name_layout": name_layout,
                "data_rows": data_rows,
                "rule_columns": rule_columns,
                "debug": debug,
            }
        )
        debug["headers"] = headers
        debug["header_end"] = header_index
        debug["data_start"] = header_index + 1
        debug["header_paths"] = header_paths_to_lists(header_paths)
        debug["name_layout"] = name_layout_to_dict(name_layout)
        debug["rule_columns"] = decisions_to_debug(rule_columns)

    # 第二阶段：先完成所有表级和字段级判断，再进入任何行提取。
    # 此处一次性完成全部表格判断，避免边判断边产出 pin 记录。
    decisions = decide_all_tables(prepared, use_semantic_classifier, include_debug)

    # 模型只提供字段判断意见；名称列的数量和分支关系由完整表头结构校验。
    # 该步骤必须发生在多封装分析之前，避免模型只选一个 name 后丢失分支。
    for item in prepared:
        decision = decisions[item["table_id"]]
        if decision.should_extract:
            decision.columns = reconcile_name_column_decisions(
                decision.columns,
                item["headers"],
                item["data_rows"],
                item["name_layout"],
            )

    # DESCRIPTION 是最终输出的附加字段，不参与“是否为引脚表”的语义判断。
    # 因此在模型/规则完成核心字段判断后，再依据已确定的完整表头统一补充，
    # 保证开启和关闭语义模型时得到相同结果。
    for item in prepared:
        decision = decisions[item["table_id"]]
        if decision.should_extract:
            decision.columns = append_description_column_decision(
                decision.columns,
                item["headers"],
            )

    # 第三阶段：先为所有已经通过判断的表完成多封装结构分析。
    # 此阶段仍然不创建 pin 记录，确保判断和提取完全分离。
    multi_package_plans: dict[int, MultiPackagePlan] = {}
    for item in prepared:
        table_decision = decisions[item["table_id"]]
        if not table_decision.should_extract:
            continue
        plan = analyze_multi_package_table(
            title=item["table"].title,
            header_rows=item["rows"][: item["header_index"] + 1],
            headers=item["headers"],
            data_rows=item["data_rows"],
            columns=table_decision.columns,
            name_layout=item["name_layout"],
        )
        multi_package_plans[item["table_id"]] = plan
        item["debug"]["multi_package_plan"] = plan_to_debug(plan)

    # 第四阶段：使用全文表格冻结物理封装槽位，并为每张已确认的引脚表
    # 绑定已有槽位。封装判断与行提取保持分离，此处仍不创建 pin 记录。
    all_catalog_tables = [
        PackageCatalogTable(
            table_id=table_id,
            page_idx=table.page_idx,
            title=table.title,
            group_context=table.group_context,
            current_chapter_titles=table.current_chapter_titles,
            # 总述表可能存在多级表头，因此目录定位读取前几行的逐列提示；
            # 完整原始 rows 仍原样交给模型，不在这里改表格结构。
            headers=catalog_header_hints(rows),
            rows=tuple(tuple(row) for row in rows),
        )
        for table_id, table, rows in parsed_tables
    ]
    target_package_tables = [
        PackageTargetTable(
            table_id=item["table_id"],
            page_idx=item["table"].page_idx,
            title=item["table"].title,
            group_context=item["table"].group_context,
            current_chapter_titles=item["table"].current_chapter_titles,
            headers=tuple(item["headers"]),
        )
        for item in prepared
        if decisions[item["table_id"]].should_extract
    ]
    package_resolution = resolve_document_package_catalog(
        all_tables=all_catalog_tables,
        target_tables=target_package_tables,
        multi_package_plans=multi_package_plans,
        source_name=source_name,
        use_semantic_classifier=use_semantic_classifier,
    )
    # 在读取任何引脚行之前建立全部外层封装桶。即使某个已确认槽位暂时
    # 没有关联到引脚表，最终 JSON 仍保留正确的封装数量。
    for declared_assignment in package_resolution.declared_assignments():
        get_package_bucket(packages, declared_assignment)

    if include_debug:
        for debug in LAST_EXTRACTION_DEBUG:
            if debug["table_id"] == 0:
                debug["package_catalog"] = {
                    "entries": [
                        {
                            "package_key": entry.package_key,
                            "identity_name": entry.identity_name,
                            "identity_aliases": entry.identity_aliases,
                            "package_type": entry.package_type,
                            "package_drawing": entry.package_drawing,
                            "pin_count": entry.pin_count,
                            "evidence_table_ids": entry.evidence_table_ids,
                        }
                        for entry in package_resolution.entries
                    ],
                    "diagnostics": package_resolution.diagnostics,
                }

    # 第五阶段：前四个判断阶段全部结束后，才允许创建 pin 记录。
    for item in prepared:
        table_decision = decisions[item["table_id"]]
        debug = item["debug"]
        debug["decision"] = decision_to_debug(table_decision)
        if not table_decision.should_extract:
            skip(debug, table_decision.reason or "table_rejected")
            continue

        # 最终 group 只取当前表格表题。章节上下文仍保存在 TableCandidate
        # 中供语义判断和封装绑定使用，不能在这里写入最终 JSON。
        group_name = clean_group_name(
            infer_group_name(item["table"].title)
            or table_decision.group
            or infer_group_name_from_headers(item["headers"])
            or "Pin/Package Table"
        )

        plan = multi_package_plans[item["table_id"]]
        extracted_count = 0

        # 真正的多封装表严格执行绑定计划。每个封装独立读取自己的 pin_no，
        # 不能再进入普通逻辑把多个编号列拼接成一个字段。
        if plan.is_multi_package:
            for bound_row in iter_bound_package_rows(plan, item["data_rows"]):
                # 先确定绑定行在当前多封装计划中的本地槽位，再读取文档级
                # 封装目录的唯一绑定。表内标签不能直接绕过目录写入输出。
                local_slot = local_package_index_for_bound_row(plan, bound_row)
                package_assignment = package_resolution.assignment_for(
                    item["table_id"],
                    local_slot,
                )
                for record in extract_records_from_bound_package_row(bound_row):
                    # 多封装绑定对象只保存 pin_no/pin_name/type。description
                    # 必须使用 row_index 回到同一原始数据行读取，不能从相邻
                    # 绑定或已经生成的记录继承。
                    description = read_optional_mapped_field(
                        item["data_rows"][bound_row.row_index],
                        table_decision.columns,
                        "description",
                    )
                    if description is not None:
                        record["description"] = description
                    if include_debug and source_name:
                        record["source"] = source_name
                    if include_debug and item["table"].page_idx is not None:
                        record["source_page"] = item["table"].page_idx + 1
                    bucket = get_package_bucket(packages, package_assignment)
                    group = get_or_create_group(bucket, group_name)
                    add_pin_record_to_group(group, record)
                    extracted_count += 1

        # 单封装表保留原有逐行提取逻辑，不经过多封装绑定。
        else:
            package_assignment = package_resolution.assignment_for(
                item["table_id"],
                0,
            )
            for row_index, row in enumerate(item["data_rows"]):
                if (
                    table_decision.included_row_indexes is not None
                    and row_index not in table_decision.included_row_indexes
                ):
                    continue
                # 完全空白行只是 HTML 排版占位，不属于项目要求保留的数据行。
                # 只要该行还有名称、类型或其他已解析内容，空 pin_no 仍会保留。
                if not any(cell.strip() for cell in row):
                    continue
                if is_group_row(row):
                    # 表内结构标题不是引脚数据，也不再创建新的输出 group。
                    continue
                for record in extract_records_from_row(row, table_decision.columns):
                    record.pop("_raw_fields", None)
                    if include_debug and source_name:
                        record["source"] = source_name
                    if include_debug and item["table"].page_idx is not None:
                        record["source_page"] = item["table"].page_idx + 1
                    bucket = get_package_bucket(packages, package_assignment)
                    group = get_or_create_group(bucket, group_name)
                    add_pin_record_to_group(group, record)
                    extracted_count += 1

        if extracted_count:
            debug.update({
                "status": "extracted",
                "pin_count": extracted_count,
                "package_assignments": [
                    {
                        "local_slot": local_slot,
                        "pkg": package_resolution.assignment_for(
                            item["table_id"],
                            local_slot,
                        ).pkg,
                        "reason": package_resolution.assignment_for(
                            item["table_id"],
                            local_slot,
                        ).reason,
                    }
                    for local_slot in (
                        range(len(plan.bindings))
                        if plan.is_multi_package
                        else range(1)
                    )
                ],
                "group": group_name,
            })
        else:
            skip(debug, "no_pin_records_after_mapping")

    return build_public_result(packages, include_debug)


def decide_all_tables(prepared: list[dict[str, Any]], use_semantic: bool, include_debug: bool) -> dict[int, TableDecision]:
    """完成全部表格判断；此函数绝不调用行提取函数。

    关闭语义判断时走规则分支；开启后并发调用模型。模型只返回是否提取
    以及目标列映射，不参与封装、分组、行提取或最终 JSON 生成。
    """

    if not prepared:
        return {}

    # 特殊表先走专用且严格的确定性处理；未命中的表才交给规则或模型。
    results: dict[int, TableDecision] = {}
    remaining: list[dict[str, Any]] = []
    for item in prepared:
        special_match = find_special_table_match(
            item["table"].title,
            item["headers"],
            item["data_rows"],
        )
        if special_match is None:
            remaining.append(item)
            continue
        results[item["table_id"]] = TableDecision(
            should_extract=special_match.should_extract,
            table_role="special_table",
            reason=special_match.handler_name,
            columns=[
                ColumnDecision(column.index, column.header, column.field_name, 10)
                for column in special_match.columns
            ],
            included_row_indexes=special_match.included_row_indexes,
        )

    if not remaining:
        return results
    if not use_semantic:
        results.update({
            item["table_id"]: decide_table_by_rules(item)
            for item in remaining
        })
        return results

    from extract.semantic_classifier import classify_table_schema

    # 默认 4 个模型请求并发；环境变量可在限流时把它调小。
    workers = max(1, int(os.getenv("EXTRACT_SCHEMA_WORKERS", "4")))
    print(
        f"语义字段判断: 候选表 {len(remaining)} 张, "
        f"特殊表直通 {len(prepared) - len(remaining)} 张, 并发 {workers}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                classify_table_schema,
                item["table"].title,
                item["headers"],
                # 把初筛后的完整二维表格交给模型，不能只发送样例行。
                item["rows"],
                header_paths_to_lists(item["header_paths"]),
                name_layout_to_dict(item["name_layout"]),
            ): item
            for item in remaining
        }
        for index, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                schema = future.result()
                decision = decision_from_schema(schema, item)
            except Exception as exc:
                decision = TableDecision(False, reason=f"semantic_classification_failed:{exc}")
            results[item["table_id"]] = decision
            print(f"语义字段判断进度: {index}/{len(remaining)}", flush=True)
    return results


def decide_table_by_rules(item: dict[str, Any]) -> TableDecision:
    """无 LLM 时的确定性表级/字段级判断。"""

    columns = keep_primary_type_decision(item["rule_columns"], item["data_rows"])
    if not is_pin_package_table(columns):
        return TableDecision(False, reason="missing_pin_number_column", columns=columns)
    return TableDecision(
        True,
        table_role="rule_pin_table",
        group=infer_group_name(item["table"].title),
        confidence=1.0,
        columns=columns,
        reason="rule_mapping_shape_valid",
    )


def decision_from_schema(schema: dict[str, Any], item: dict[str, Any]) -> TableDecision:
    """消费模型的最小返回值，不再读取角色、封装名、组名或置信度。"""

    # 模型层只拥有这一个表级开关；模型拒绝后不再叠加角色判断。
    if not bool(schema.get("should_extract")):
        return TableDecision(False, reason="semantic_rejected")

    # 列映射完全来自模型返回的 column_index 和 field。
    columns = build_schema_column_decisions(schema, item["headers"])
    # 模型输出属于不可信的字段判断结果。物理引脚记录必须有 pin_no 映射；
    # pin_name 则允许缺失，后续逐行创建记录后统一补成 Reserved。
    if not has_pin_number_column(columns):
        return TableDecision(
            False,
            table_role="semantic_selected",
            reason="semantic_missing_pin_no_column",
            columns=columns,
        )
    return TableDecision(
        True,
        table_role="semantic_selected",
        reason="semantic_selected",
        columns=columns,
    )


# ---------------------------------------------------------------------------
# 表格判断：只判断“是不是目标表”，不生成输出记录
# ---------------------------------------------------------------------------


def is_pinout_matrix_table(rows: list[list[str]], title: str = "") -> bool:
    """排除坐标矩阵/引脚排列图对应的表格。

    这类表格常见形式是：表头包含 1、2、3...，第一列数据是 A、B、C...，
    单元格内容是坐标交叉关系。它不是“每行一个物理引脚”的表，不能把
    A/B/C 误当成 pin_no，也不能把整行矩阵内容当成 pin_name。
    """

    text = normalize_header(title)
    if any(word in text for word in ("pin configuration", "pinout", "pin map", "pin assignment")):
        title_signal = True
    else:
        title_signal = False
    if len(rows) < 3:
        return False
    header = [normalize_header(cell) for cell in rows[0]]
    explicit_pin_header = any(
        any(token in cell for token in ("pin no", "pin number", "ball number", "terminal no", "signal name"))
        for cell in header
    )
    if explicit_pin_header and not title_signal:
        return False

    numeric_header = sum(bool(re.fullmatch(r"\d{1,3}", cell)) for cell in header if cell)
    row_label_hits = 0
    for row in rows[1: min(len(rows), 12)]:
        first = normalize_header(row[0]) if row else ""
        if re.fullmatch(r"[a-z]", first):
            row_label_hits += 1
    return (title_signal and numeric_header >= 2 and row_label_hits >= 2) or (numeric_header >= 4 and row_label_hits >= 3 and not explicit_pin_header)


def is_loose_candidate(title: str, headers: list[str], rows: list[list[str]]) -> bool:
    """召回候选表；宽松，但只负责决定是否值得做字段判断。"""

    header_text = normalize_header(" ".join(headers))
    title_text = normalize_header(title)
    if is_ordering_table(headers):
        return False
    if any(word in header_text for word in ("register description", "offset address", "timing requirement")):
        return False
    if any(word in header_text for word in ("pin", "ball", "terminal", "signal", "引脚", "端子", "信号")):
        return True
    if any(word in title_text for word in ("pin attributes", "terminal functions", "signal descriptions", "connectivity requirements")):
        return True
    return bool(re.search(r"\b[A-Z]{1,2}\d{1,3}\b", " ".join(" ".join(row) for row in rows[:8])))


def is_non_physical_port_function_table(title: str, headers: list[str]) -> bool:
    """排除功能复用表，但不排除有明确物理 pin 编号的表。"""

    h = normalize_header(" ".join(headers))
    t = normalize_header(title)
    physical = any(word in h for word in ("pin no", "pin number", "ball number", "terminal no", "terminal number", "引脚编号", "端子编号"))
    if physical:
        return False
    return bool(re.search(r"\bport\s+p[a-z0-9.]*\b", t) or any(word in h + " " + t for word in ("pin mux", "pinmux", "alternate function", "default mapping")))


def is_ordering_table(headers: list[str]) -> bool:
    """根据表头组合判断是否是器件订货/包装信息表。"""
    text = normalize_header(" ".join(headers))
    words = ("orderable device", "package qty", "package type", "package drawing", "eco plan", "lead finish", "msl peak temp")
    return "orderable device" in text and sum(word in text for word in words) >= 2


def has_pin_number_column(columns: list[ColumnDecision]) -> bool:
    """检查字段判断结果是否包含生成物理引脚记录所需的编号列。"""

    fields = {normalize_field_name(c.field_name) for c in columns}
    return bool(fields & {"pin_no", "package_pin_no"})


def is_pin_package_table(columns: list[ColumnDecision]) -> bool:
    """检查字段映射是否具有真实引脚表的最小结构。"""

    # pin_name 不是最低结构要求：只有编号列的 Reserved/NC 表仍要输出，
    # 名称由最终清洗阶段补成 Reserved。
    if not has_pin_number_column(columns):
        return False
    number = [c for c in columns if normalize_field_name(c.field_name) in {"pin_no", "package_pin_no"}]
    return any(classify_header(c.raw_header)[1] >= 3 for c in number)


# ---------------------------------------------------------------------------
# 字段判断：规则或 LLM 只返回 ColumnDecision
# ---------------------------------------------------------------------------


def classify_columns(headers: list[str], rows: list[list[str]], title: str = "") -> list[ColumnDecision]:
    """用表头优先、列值辅助的规则判断每一列的语义。"""
    # decisions 只记录列映射，不包含任何从数据行生成的 pin 记录。
    decisions = []
    width = max([len(headers)] + [len(row) for row in rows[:30]] or [0])
    for index in range(width):
        header = headers[index] if index < len(headers) else ""
        values = [row[index] for row in rows[:30] if index < len(row)]
        field, header_score = classify_header(header)
        value_field, value_score = classify_values(values)
        if not field and value_field:
            field = value_field
        if field and header_score + value_score > 0:
            decisions.append(ColumnDecision(index, header, field, header_score + value_score))
    return keep_primary_type_decision(decisions, rows)


def build_schema_column_decisions(schema: dict[str, Any], headers: list[str]) -> list[ColumnDecision]:
    """把模型返回的最小列映射转换成内部对象，不推断其他字段。"""
    result = []
    for item in schema.get("columns") or []:
        index = parse_column_index(item.get("column_index"))
        field = str(item.get("field") or "ignore").strip()
        if index is None or field == "ignore":
            continue
        result.append(ColumnDecision(index, safe_header(headers, index), field, 10))
    return result


def reconcile_name_column_decisions(
    columns: list[ColumnDecision],
    headers: list[str],
    data_rows: list[list[str]],
    name_layout: NameColumnLayout,
) -> list[ColumnDecision]:
    """按完整表头结构校验模型/规则返回的名称列。

    模型只负责语义判断，不能覆盖确定性的多层表头关系。分支名称列必须
    每个分支保留一个；普通等价名称列只能保留一个。该函数只调整
    ``pin_name``，不会创建、删除 pin_no/type/description 映射。
    """

    selected_name_indexes = {
        column.index
        for column in columns
        if normalize_field_name(column.field_name) == "pin_name"
    }
    other_columns = [
        column
        for column in columns
        if normalize_field_name(column.field_name) != "pin_name"
    ]

    if name_layout.mode == "package_branches":
        # 每个结构分支独立选择一列。优先采用模型在该分支内已经选中的列；
        # 模型漏选整个分支时，从该分支的候选列中按完整度确定性恢复。
        chosen_indexes = [
            choose_best_name_column(
                branch,
                selected_name_indexes,
                data_rows,
            )
            for branch in name_layout.branches
        ]
    elif name_layout.mode == "equivalent_names":
        # BALL NAME、SIGNAL NAME 等普通等价字段只能选一列。模型选了多列
        # 也不拼接；优先在模型所选范围内比较完整度，同分取最左列。
        candidates = [
            index
            for index in name_layout.name_column_indexes
            if index in selected_name_indexes
        ] or list(name_layout.name_column_indexes)
        chosen_indexes = (
            [best_column_by_completeness(candidates, data_rows)]
            if candidates
            else []
        )
    else:
        # 单名称结构若模型重复返回同一语义的多个列，也只保留结构中最可信
        # 的一列；模型完全没有选择 pin_name 时仍允许后续填充 Reserved。
        candidates = sorted(selected_name_indexes)
        if len(candidates) <= 1:
            return deduplicate_column_decisions(columns)
        chosen_indexes = [
            best_column_by_completeness(candidates, data_rows)
        ]

    result = list(other_columns)
    for column_index in chosen_indexes:
        result.append(
            ColumnDecision(
                column_index,
                safe_header(headers, column_index),
                "pin_name",
                10,
            )
        )
    return deduplicate_column_decisions(result)


def choose_best_name_column(
    branch: NameColumnBranch,
    selected_name_indexes: set[int],
    data_rows: list[list[str]],
) -> int:
    """为一个表头分支选择唯一名称列，模型漏选时仍保留该结构分支。"""

    selected = [
        index
        for index in branch.column_indexes
        if index in selected_name_indexes
    ]
    candidates = selected or list(branch.column_indexes)
    return best_column_by_completeness(candidates, data_rows)


def best_column_by_completeness(
    column_indexes: list[int],
    data_rows: list[list[str]],
) -> int:
    """按非空数据数量选择整列；数量相同时固定选择最左列。"""

    return max(
        column_indexes,
        key=lambda index: (
            sum(
                bool(row[index].strip())
                for row in data_rows
                if index < len(row)
            ),
            -index,
        ),
    )


def deduplicate_column_decisions(
    columns: list[ColumnDecision],
) -> list[ColumnDecision]:
    """按列号和标准字段去重，同时保持原有列顺序。"""

    result = []
    seen: set[tuple[int, str]] = set()
    for column in sorted(columns, key=lambda item: item.index):
        key = (column.index, normalize_field_name(column.field_name))
        if key in seen:
            continue
        seen.add(key)
        result.append(column)
    return result


def append_description_column_decision(
    columns: list[ColumnDecision],
    headers: list[str],
) -> list[ColumnDecision]:
    """表头存在 DESCRIPTION 时补充唯一的 description 列映射。

    description 不参与表格有效性判断，也不交给模型选择。这里在核心字段已经
    冻结后执行，避免辅助字段影响 pin_no/pin_name/type 的判断结果。
    """

    result = list(columns)
    if any(normalize_field_name(column.field_name) == "description" for column in result):
        return result
    for index, header in enumerate(headers):
        if is_description_header(header):
            result.append(ColumnDecision(index, header, "description", 10))
            break
    return result


def is_description_header(value: str) -> bool:
    """识别 DESCRIPTION 列，并隔离误附加在表头后的首行描述文字。

    正常情况下表头选择应停在真正的 DESCRIPTION 表头。这里仍接受
    ``DESCRIPTION <首行文字>``，是为了保证异常组合表头不会再被解释成
    package/type 等其他字段；附加文字只影响 description，不改变列语义。
    """

    header = normalize_header(value)
    header = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", header).strip()
    return bool(
        re.match(
            r"^(?:(?:pin|signal|terminal)\s+)?description(?:\s|$)",
            header,
        )
    )


def read_optional_mapped_field(
    row: list[str],
    columns: list[ColumnDecision],
    field_name: str,
) -> str | None:
    """读取可选映射字段；未映射返回 None，已映射但单元格为空返回空串。"""

    for column in columns:
        if normalize_field_name(column.field_name) != field_name:
            continue
        return row[column.index].strip() if column.index < len(row) else ""
    return None


def keep_primary_type_decision(columns: list[ColumnDecision], rows: list[list[str]]) -> list[ColumnDecision]:
    """多个 type 列时只保留最接近 signal/pin 语义的那一列。"""

    types = [c for c in columns if normalize_field_name(c.field_name) == "type"]
    if len(types) <= 1:
        return columns
    best = max(types, key=lambda c: score_type_column(c, rows))
    return [c for c in columns if normalize_field_name(c.field_name) != "type" or c.index == best.index]


def score_type_column(column: ColumnDecision, rows: list[list[str]]) -> tuple[int, int]:
    """给 type 列评分：signal/pin 语义高于 buffer、power 等辅助语义。"""
    header = normalize_header(column.raw_header)
    score = column.score
    if any(word in header for word in ("signal type", "pin type", "terminal type", "io type", "i o type")):
        score += 20
    if header in {"type", "i o", "io", "i/o"}:
        score += 10
    if any(word in header for word in ("buffer type", "buffer", "reset state", "power source")):
        score -= 15
    values = [row[column.index] for row in rows[:30] if column.index < len(row)]
    score += 3 * sum(looks_like_signal_type(value) for value in values[:20])
    return score, -column.index


def classify_header(header: str) -> tuple[str, int]:
    """根据表头文字返回标准字段名和表头匹配分数。"""
    h = normalize_header(header)
    if not h:
        return "", 0
    # DESCRIPTION 只在核心字段判断结束后由
    # append_description_column_decision() 补充。必须先截断其余规则，
    # 否则首行描述里的 package/pin/type 等普通词会污染字段评分。
    if is_description_header(h):
        return "", 0
    if any(word in h for word in ("pin no", "pin number", "ball no", "ball number", "terminal no", "terminal number", "引脚编号", "端子编号")) or re.search(r"(?:引脚|端子|球)\s+编号", h):
        return "pin_no", 5
    # Reserved/NC 表常把物理编号列简写为 PINS、BALLS 或 TERMINALS。
    if h in {"pins", "balls", "terminals"}:
        return "pin_no", 4
    if any(word in h for word in ("pin name", "ball name", "signal name", "terminal name", "引脚名称", "信号名称", "引脚名")) or re.search(r"(?:引脚|信号|端子|球)\s+名称", h):
        return "pin_name", 5
    if any(word in h for word in ("signal type", "pin type", "terminal type", "io type", "i o type", "引脚类型", "信号类型")):
        return "type", 5
    if h in {"no", "no.", "number", "signal no", "signal no."}:
        return "pin_no", 4
    if h in {"signal", "name"}:
        return "pin_name", 3
    if h in {"type", "io", "i o", "i/o", "类型"} or h.endswith(" type"):
        return "type", 3
    if "package" in h and "pin" not in h:
        return "package", 2
    return "", 0


def classify_values(values: list[str]) -> tuple[str, int]:
    """用列值形态补充表头判断，主要识别编号和值类型列。"""
    sample = [str(value).strip() for value in values if str(value).strip()][:20]
    if not sample:
        return "", 0
    if sum(looks_like_pin_list(v) for v in sample) >= max(2, len(sample) // 3):
        return "pin_no", 3
    if sum(looks_like_signal_type(v) for v in sample) >= max(2, len(sample) // 3):
        return "type", 2
    return "", 0


# ---------------------------------------------------------------------------
# 行提取和清洗：这里不再决定表格是否有效
# ---------------------------------------------------------------------------


def extract_records_from_bound_package_row(
    bound_row: BoundPackageRow,
) -> list[dict[str, Any]]:
    """把多封装模块绑定的一行转换为项目统一的 pin 记录。

    package 与列/行关系已经由多封装模块确定；本函数只复用项目统一的
    pin_no 列表/范围展开和 pin_name 按位置对应规则，不能重新判断封装。
    """

    # 绑定计划已经证明该列是该封装的 pin_no。单元格为空属于原始数据，
    # 不能再通过空列表静默丢行，因此用一个空编号保留该条记录。
    pin_numbers = split_pin_numbers(bound_row.pin_no) or [""]
    pin_names = split_parallel_pin_names(
        bound_row.pin_name,
        len(pin_numbers),
        pin_no_value=bound_row.pin_no,
    )
    records = []
    for index, pin_no in enumerate(pin_numbers):
        record: dict[str, Any] = {
            "pin_no": pin_no,
            "pin_name": pin_names[index] if pin_names else bound_row.pin_name,
        }
        if bound_row.pin_type:
            record["type"] = bound_row.pin_type
        records.append(record)
    return records


def extract_records_from_row(row: list[str], columns: list[ColumnDecision]) -> list[dict[str, Any]]:
    """按已确定的列映射读取一整行；空值不触发表级过滤。

    这里是唯一负责把一行转换为若干 pin 记录的函数；它不重新判断表格
    是否有效，也不删除已经被字段判断选中的列。
    """

    # fields 是标准字段值；package_columns 单独保存多封装专属 pin 列。
    fields: dict[str, str] = {}
    package_columns: list[str] = []
    # raw_fields 仅用于 debug，不参与最终字段判断和输出。
    raw_fields: dict[str, str] = {}
    for column in columns:
        value = row[column.index].strip() if column.index < len(row) else ""
        raw_fields[column.raw_header or f"column_{column.index + 1}"] = value
        field_name = normalize_field_name(column.field_name)
        if column.field_name == "package_pin_no":
            # 这里只保存编号值，不保留封装表头文字。正常情况下多封装结构
            # 已在上一阶段接管；该分支只是兼容既有字段映射。
            package_columns.append(value)
        elif field_name:
            # 同一个字段有多个列时保留原始信息，不在这里合并 pin 记录。
            fields[field_name] = merge_field_value(fields.get(field_name, ""), value)

    records = []
    if package_columns:
        # 封装专属编号列优先：每个封装列分别生成自己的 pin 记录。
        for value in package_columns:
            # 列映射已经确定；空单元格仍要生成 pin_no="" 的记录。
            pin_numbers = split_pin_numbers(value) or [""]
            pin_names = split_parallel_pin_names(
                fields.get("pin_name", ""),
                len(pin_numbers),
                pin_no_value=value,
            )
            for index, pin_no in enumerate(pin_numbers):
                record = dict(fields)
                record["pin_no"] = pin_no
                if pin_names:
                    # 只有数量完全对应时，才覆盖整行共享的 pin_name。
                    record["pin_name"] = pin_names[index]
                record["_raw_fields"] = raw_fields
                records.append(record)
        if records:
            return records

    pin_value = fields.get("pin_no", "")
    # 表级判断已保证存在 pin_no 映射。这里的空字符串只表示当前数据行
    # 编号为空，不代表字段缺失，因此仍保留一条空编号记录。
    pin_numbers = split_pin_numbers(pin_value) or [""]
    pin_names = split_parallel_pin_names(
        fields.get("pin_name", ""),
        len(pin_numbers),
        pin_no_value=pin_value,
    )
    for index, pin_no in enumerate(pin_numbers):
        record = dict(fields)
        record["pin_no"] = pin_no
        # 只有 pin_name 的显式分隔项数量与 pin_no 数量完全一致时，
        # 才按位置对应拆开；数量不一致时保留原始 pin_name，避免误拆。
        if pin_names:
            record["pin_name"] = pin_names[index]
        record["_raw_fields"] = raw_fields
        records.append(record)
    return records


def split_pin_numbers(value: str) -> list[str]:
    """拆分 pin_no 的显式列表、连字符范围和方括号范围。

    例如 ``A1-A5`` 会展开为 A1、A2、A3、A4、A5；
    ``L[7:12]`` 会展开为 L7、L8、L9、L10、L11、L12；
    ``A1-C3`` 前后字母前缀不同，不满足范围规则，会保留原值。
    """

    value = plain_text(str(value or "")).strip()
    if not value:
        return []

    # 先保护带空格的范围，避免普通空格分隔逻辑把
    # A1 - A5 或 L[7 : 12] 拆成多个无意义片段。
    protected_ranges: list[str] = []

    def protect_range(match: re.Match[str]) -> str:
        """把范围暂存起来，并返回不会被普通分隔符拆开的占位符。"""

        protected_ranges.append(match.group(0))
        return f"__PIN_RANGE_{len(protected_ranges) - 1}__"

    protected_value = re.sub(
        (
            r"(?<![A-Za-z0-9_])(?:"
            r"[A-Za-z]+\s*\d+\s*-\s*[A-Za-z]+\s*\d+"
            r"|[A-Za-z]+\s*\[\s*\d+\s*:\s*\d+\s*\]"
            r")(?![A-Za-z0-9_])"
        ),
        protect_range,
        value,
    )
    parts = [part.strip() for part in re.split(r"[\s,，;/／、|]+", protected_value) if part.strip()]
    if not parts:
        parts = [value]

    expanded = []
    for part in parts:
        protected_match = re.fullmatch(r"__PIN_RANGE_(\d+)__", part)
        original_part = (
            protected_ranges[int(protected_match.group(1))]
            if protected_match
            else part
        )
        # 每个 token 只进入一种范围解析器。方括号语法优先判断，
        # 未命中时再沿用原有的 A1-A5 连字符范围逻辑。
        if re.fullmatch(
            r"[A-Za-z]+\s*\[\s*\d+\s*:\s*\d+\s*\]",
            original_part,
        ):
            expanded.extend(expand_bracketed_pin_range(original_part))
        else:
            expanded.extend(expand_same_prefix_pin_range(original_part))
    return expanded


def expand_bracketed_pin_range(value: str) -> list[str]:
    """展开形如 L[7:12] 的 BGA 引脚范围，其他文本保持原样。"""

    match = re.fullmatch(
        r"([A-Za-z]+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]",
        value.strip(),
    )
    if not match:
        return [value]

    prefix, start_number, end_number = match.groups()
    start = int(start_number)
    end = int(end_number)

    # 方括号范围允许正序和倒序；步长由两个端点的顺序确定。
    step = 1 if end >= start else -1
    if abs(end - start) > 1000:
        return [value]

    # 端点带前导零时保留原宽度，例如 A[01:03] -> A01、A02、A03。
    width = max(len(start_number), len(end_number))
    preserve_width = start_number.startswith("0") or end_number.startswith("0")
    return [
        f"{prefix}{str(number).zfill(width) if preserve_width else number}"
        for number in range(start, end + step, step)
    ]


def expand_same_prefix_pin_range(value: str) -> list[str]:
    """展开形如 A1-A5 的范围，其他文本保持原样。"""

    match = re.fullmatch(
        r"([A-Za-z]+)\s*(\d+)\s*-\s*([A-Za-z]+)\s*(\d+)",
        value.strip(),
    )
    if not match:
        return [value]

    # 四个分组分别是起始前缀、起始数字、结束前缀、结束数字。
    start_prefix, start_number, end_prefix, end_number = match.groups()
    if start_prefix.upper() != end_prefix.upper():
        return [value]

    start = int(start_number)
    end = int(end_number)
    if end < start:
        return [value]

    # 防止异常文本触发超大范围展开。
    if end - start > 1000:
        return [value]

    prefix = start_prefix
    return [f"{prefix}{number}" for number in range(start, end + 1)]


def split_parallel_pin_names(
    value: str,
    expected_count: int,
    pin_no_value: str = "",
) -> list[str]:
    """按 pin_no 数量拆分同一行中的 pin_name。

    具体的 ``<br>`` 结构判断放在 parallel_cell_splitter.py。本函数保留
    主提取器中的统一调用入口，避免行提取分支各自实现不同拆分规则。
    """

    return split_parallel_pin_names_by_structure(
        pin_name_value=value,
        pin_no_value=pin_no_value,
        expected_count=expected_count,
        # 复用主提取器唯一的 pin_no 规则，包括显式列表、A1-A5 和
        # L[7:12]，避免并行拆分模块维护另一套编号解释逻辑。
        split_pin_numbers=split_pin_numbers,
    )


def normalize_pin_record(record: dict[str, Any]) -> dict[str, Any]:
    """对已生成的单条记录执行项目规定的最终清洗。"""
    result = dict(record)
    result["pin_no"] = plain_text(str(result.get("pin_no", ""))).strip()
    result["pin_name"] = clean_pin_name(result.get("pin_name", ""))
    return result


def clean_pin_name(value: Any) -> str:
    """清理名称尾部标记，并把空名称统一成 Reserved。"""
    value = plain_text(str(value or "")).strip()
    value = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", value)
    value = re.sub(r"\s*[（(]\s*continued\s*[）)]\s*$", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip() or "Reserved"


def add_pin_record_to_group(group: ExtractedGroup, record: dict[str, Any]) -> None:
    """把单条 pin 追加到分组，刻意不按 pin_no 去重。"""
    # 项目规则：每条行记录独立保留，绝不按 pin_no 去重或合并。
    group.pin_list.append(normalize_pin_record(record))


# ---------------------------------------------------------------------------
# 表格读取、标题和输出分组
# ---------------------------------------------------------------------------


TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
BALL_TOKEN_RE = re.compile(r"\b[A-Z]{1,2}\d{1,3}\b")
TYPE_VALUES = {"i", "o", "io", "i/o", "i/o/z", "oz", "od", "odz", "p", "ipu", "ipd", "analog", "digital", "power", "ground", "gnd", "supply", "input", "output"}


def parse_html_table(html: str) -> list[list[str]]:
    """把 HTML table 转成二维数组，并保留单元格内的显式 ``<br>``。"""
    rows = []
    for tr in TR_RE.finditer(html):
        # parse_html_cell_text() 只保留明确的 <br> 结构；后续表头判断仍会
        # 通过 plain_text() 折叠换行，不会改变模型看到的表头语义。
        row = [parse_html_cell_text(match.group(2)) for match in CELL_RE.finditer(tr.group(2))]
        if row:
            rows.append(row)
    return rows


def choose_header_row(rows: list[list[str]], title: str = "", semantic: bool = False) -> tuple[int, list[str]]:
    """先找字段语义种子行，再通过结构关系确定完整表头边界。"""
    best = (-1, -1, [])
    # 十二行只是异常HTML的防护上限；最终边界仍由字段轴、分支标签和后续
    # 数据一致性决定，不依赖某个PDF的固定表头层数。
    for index in range(min(12, len(rows))):
        headers = build_combined_headers(rows, index)
        score = sum(classify_header(header)[1] for header in headers)
        if score > best[1]:
            best = (index, score, headers)
    if best[1] < (2 if semantic else 4):
        return -1, []
    boundary = resolve_header_boundary(rows, best[0])
    headers = build_combined_headers(rows, boundary.header_end)
    return boundary.header_end, headers


def should_retry_spanned_header(
    table: TableCandidate,
    rough_rows: list[list[str]],
) -> bool:
    """仅为确有多层表头证据的表启用 span-aware 表头重试。

    该函数只决定是否值得重新解析表头，不认定表格有效，也不创建字段映射。
    重试后的表仍须依次通过候选表、模型和字段完整性判断。
    """

    html = str(table.html or "")
    if not re.search(r"\b(?:rowspan|colspan)\s*=", html, re.IGNORECASE):
        return False

    # 只读取标题和前四行作为结构锚点，禁止用数据区内容反向证明表头。
    header_window = " ".join(
        [table.title]
        + [cell for row in rough_rows[:4] for cell in row]
    )
    normalized = normalize_header(header_window)
    has_pin_axis = bool(
        re.search(r"\b(?:pin|ball|terminal)\b", normalized)
        or any(term in normalized for term in ("引脚", "端子", "球"))
    )
    has_field_leaf = bool(
        re.search(r"\b(?:name|type|description|no|number)\b", normalized)
        or any(
            term in normalized
            for term in ("名称", "类型", "说明", "描述", "编号")
        )
    )
    return has_pin_axis and has_field_leaf


def build_combined_headers(rows: list[list[str]], header_index: int) -> list[str]:
    """合并多层表头，使跨行表头在字段判断时成为一个完整文字。"""
    width = max((len(row) for row in rows[: header_index + 1]), default=0)
    result = []
    for index in range(width):
        parts = []
        for row in rows[: header_index + 1]:
            # 表头中的 <br> 只负责视觉排版，在字段判断时仍折叠成普通空格。
            part = plain_text(row[index]) if index < len(row) else ""
            if part and part not in parts:
                parts.append(part)
        result.append(" ".join(parts))
    return result


def is_group_row(row: list[str]) -> bool:
    """识别只有一个重复文本的分组标题行，而不是普通数据行。"""
    values = [cell.strip() for cell in row if cell.strip()]
    return bool(values) and (len(values) == 1 or len(set(values)) == 1) and not looks_like_pin_list(values[0])


def infer_group_name(text: str) -> str:
    """清理已经绑定到候选表的标题，不再检查标题包含哪些语义词。"""
    return clean_group_name(text)


def extract_table_title(text: str) -> str:
    """兼容旧调用：识别带 Table/表 和表号的明确表题。"""

    return extract_numbered_table_title(text)


def infer_group_name_from_headers(headers: list[str]) -> str:
    """当附近没有标题时，根据表头组合生成保守的组名。"""
    h = normalize_header(" ".join(headers))
    if "connection requirements" in h or "connectivity requirements" in h:
        return "Connectivity Requirements"
    if "description" in h and ("function" in h or "signal type" in h):
        return "Signal Descriptions"
    if "pin" in h or "ball" in h or "terminal" in h:
        return "Pin Attributes"
    return ""


def clean_group_name(value: str) -> str:
    """逐行清理 group，保留章节上下文中的换行和 Table/表格编号。

    同一张跨页表的后续标题通常只多出 ``(continued)``。删除该标记后，
    原表和续表仍会得到完全相同的 group 名；每一行标题独立清理，不能再用
    ``\\s+`` 把标题之间的换行折叠成普通空格。
    """
    return join_group_titles(value)


def local_package_index_for_bound_row(
    plan: MultiPackagePlan,
    bound_row: BoundPackageRow,
) -> int:
    """把多封装绑定行映射到它在当前表中的封装索引。

    ``binding.package`` 只是多封装模块定位列/行时使用的内部标签。这里仅用
    它在当前 plan 中查找本表索引；最终 pkg 由文档级槽位提供真实物理封装名，
    名称缺失时才按槽位顺序使用 a/b/c。
    """

    for slot, binding in enumerate(plan.bindings):
        if binding.package == bound_row.package:
            return slot
    raise ValueError(
        f"多封装绑定行没有对应的本表封装索引: {bound_row.package!r}"
    )


def get_package_bucket(
    packages: dict[str, dict[str, Any]],
    assignment: PackageAssignment,
) -> dict[str, Any]:
    """按文档级 package_key 获取输出桶；公开名称与内部 key 分开保存。"""

    key = str(assignment.package_key)
    if key not in packages:
        packages[key] = {
            "package_key": key,
            "pkg": str(assignment.pkg),
            "_groups": {},
        }
    return packages[key]


def get_or_create_group(bucket: dict[str, Any], name: str) -> ExtractedGroup:
    """在封装桶中获取或创建一个表格/小分组。"""
    name = clean_group_name(name) or "Pin/Package Table"
    if name not in bucket["_groups"]:
        bucket["_groups"][name] = ExtractedGroup(name)
    return bucket["_groups"][name]


def build_public_result(
    packages: dict[str, dict[str, Any]],
    include_debug: bool,
) -> list[dict[str, Any]]:
    """按冻结槽位顺序生成公开 JSON，包括暂时没有引脚记录的槽位。"""

    result = []
    for bucket in packages.values():
        groups = [{"group": group.group, "pin_list": group.pin_list} for group in bucket["_groups"].values() if group.pin_list]
        item = {"pkg": bucket["pkg"], "group_list": groups}
        if include_debug:
            item["package_key"] = bucket["package_key"]
        result.append(item)
    return append_duplicate_pkg_suffixes(result)


def append_duplicate_pkg_suffixes(
    result: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为最终 JSON 中重复的 pkg 名称按出现顺序追加数字后缀。

    该函数只修改公开输出中的 ``pkg`` 字符串，不修改内部 ``package_key``、
    group 或 pin_list。因此多个同名封装槽位仍然保留各自独立的数据。
    """

    # 先完成全量计数，确保只出现一次的 pkg 不会被无意义地追加 ``1``。
    package_name_counts = Counter(str(item.get("pkg", "")) for item in result)
    package_name_indexes: Counter[str] = Counter()

    for item in result:
        package_name = str(item.get("pkg", ""))
        if package_name_counts[package_name] <= 1:
            continue

        # 重复名称从第一个槽位开始编号，保证输出中不存在两个相同 pkg。
        package_name_indexes[package_name] += 1
        item["pkg"] = f"{package_name}{package_name_indexes[package_name]}"

    return result


# ---------------------------------------------------------------------------
# 候选表来源
# ---------------------------------------------------------------------------


def iter_table_candidates(middle_json: dict[str, Any]) -> list[TableCandidate]:
    """按页面阅读顺序从 middle_json 收集 HTML 表格及附近标题。"""
    result = []
    previous_table_title = ""
    # pending_texts 始终只保存上一张表结束后到当前扫描位置的文本。
    # 表格一旦消费该窗口就清空，避免旧章节表题无限向后传播。
    pending_texts: list[str] = []
    title_context = GroupTitleContextTracker()
    for page in middle_json.get("pdf_info", []):
        page_idx = page.get("page_idx") if isinstance(page, dict) else None
        for span in iter_spans_in_reading_order(page):
            html = span.get("html") if isinstance(span, dict) else None
            text = plain_text((span.get("content") or span.get("text") or "") if isinstance(span, dict) else "")
            if isinstance(html, str) and "<table" in html.lower():
                table_title = resolve_table_title(
                    pending_texts,
                    previous_table_title,
                )
                result.append(
                    TableCandidate(
                        html,
                        page_idx if isinstance(page_idx, int) else None,
                        table_title,
                        title_context.build_group_context(table_title),
                        tuple(title_context.previous_chapter_titles),
                        tuple(title_context.current_chapter_titles),
                    )
                )
                previous_table_title = table_title
                pending_texts = []
            elif text:
                # 章节状态与表题状态相互独立；Table/Figure 标题会由上下文
                # 模块排除，正文也不会进入章节标题列表。
                title_context.observe(text)
                pending_texts.append(text)
    return result


def iter_table_candidates_from_markdown(markdown: str) -> list[TableCandidate]:
    """从最终 Markdown 中收集 HTML 表格，并绑定最近的标题文本。"""
    result = []
    table_re = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
    cursor = 0
    previous_table_title = ""
    title_context = GroupTitleContextTracker()
    for match in table_re.finditer(markdown):
        before = markdown[cursor : match.start()]
        # 保留原始 Markdown 行中的 #，供章节标题和独立局部标题判断使用。
        texts = [
            line.strip()
            for line in before.splitlines()
            if plain_text(line).strip()
        ]
        for text in texts:
            # 最终 Markdown 只信任带 # 的明确标题，避免把目录项、编号列表
            # 或正文中的数字开头句子错误收进章节上下文。
            title_context.observe(text, require_markdown_heading=True)
        table_title = resolve_table_title(texts, previous_table_title)
        result.append(
            TableCandidate(
                match.group(0),
                None,
                table_title,
                title_context.build_group_context(table_title),
                tuple(title_context.previous_chapter_titles),
                tuple(title_context.current_chapter_titles),
            )
        )
        previous_table_title = table_title
        cursor = match.end()
    return result


def iter_spans_in_reading_order(value: Any):
    """递归遍历 MinerU 嵌套结构，按原始阅读顺序返回文本 span。"""
    if isinstance(value, dict):
        for key in ("spans", "para_blocks", "blocks", "lines"):
            child = value.get(key)
            if isinstance(child, list):
                for item in child:
                    yield from iter_spans_in_reading_order(item)
        if any(key in value for key in ("html", "content", "text")):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_spans_in_reading_order(item)


# ---------------------------------------------------------------------------
# 小工具、输出和调试
# ---------------------------------------------------------------------------


def normalize_field_name(name: str) -> str:
    """把 ball/terminal/signal 等同义字段归一到 pin 输出字段。"""
    if name in {"ball_no", "terminal_no"}:
        return "pin_no"
    if name in {"ball_name", "signal_name", "terminal_name", "pad_name"}:
        return "pin_name"
    if name == "io_type":
        return "type"
    return name


def looks_like_pin_list(value: str) -> bool:
    """判断一个单元格是否像编号列表，用于候选表和分组行识别。"""
    value = str(value).strip()
    return bool(re.fullmatch(r"\d{1,4}", value) or BALL_TOKEN_RE.search(value))


def looks_like_signal_type(value: str) -> bool:
    """判断单元格值是否像 I/O、Power、Analog 等类型值。"""
    return normalize_header(str(value)).replace(" ", "") in TYPE_VALUES


def merge_field_value(left: str, right: str) -> str:
    """多个列映射到同一字段时保留两个非重复值。"""
    if not left:
        return right
    if not right or left == right:
        return left
    return f"{left} | {right}"


def normalize_header(value: str) -> str:
    """统一表头比较格式，但不修改最终输出内容。"""
    value = plain_text(value).lower()
    value = re.sub(r"\[[^]]+\]", " ", value)
    value = re.sub(r"[$\\^†‡*]+", " ", value)
    value = re.sub(r"[_\-/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def plain_text(value: str) -> str:
    """去除 HTML 标签、实体和多余空白，得到用于判断的文本。"""
    value = re.sub(r"<br\s*/?>", " ", str(value), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", html_lib.unescape(TAG_RE.sub("", value))).strip()


def parse_column_index(value: Any) -> int | None:
    """把模型返回的列编号安全转换为非负整数。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def safe_header(headers: list[str], index: int) -> str:
    """安全读取列名；模型给出越界列号时生成占位列名。"""
    return headers[index] if 0 <= index < len(headers) else f"column_{index + 1}"


def skip(debug: dict[str, Any], reason: str) -> None:
    """统一记录表格被跳过的状态和原因。"""
    debug["status"] = "skipped"
    debug["skip_reason"] = reason


def decisions_to_debug(columns: list[ColumnDecision]) -> list[dict[str, Any]]:
    """把列决策对象转换成可写入 debug JSON 的字典。"""
    return [{"index": c.index, "header": c.raw_header, "field": c.field_name, "score": c.score} for c in columns]


def decision_to_debug(decision: TableDecision) -> dict[str, Any]:
    """把表格决策对象转换成可写入 debug JSON 的字典。"""
    return {
        "should_extract": decision.should_extract,
        "table_role": decision.table_role,
        "group": decision.group,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "columns": decisions_to_debug(decision.columns),
        "included_row_indexes": (
            sorted(decision.included_row_indexes)
            if decision.included_row_indexes is not None
            else None
        ),
    }


def get_last_extraction_debug() -> list[dict[str, Any]]:
    """返回最近一次提取的表级判断日志。"""
    return LAST_EXTRACTION_DEBUG


def strip_debug_fields(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删除 source 等调试字段，生成用户最终看到的 JSON。"""
    clean = []
    for package in result:
        item = {"pkg": package.get("pkg", ""), "group_list": []}
        for group in package.get("group_list", []):
            pins = []
            for pin in group.get("pin_list", []):
                pins.append({k: v for k, v in pin.items() if k not in {"source", "source_page", "raw_fields"}})
            if pins:
                item["group_list"].append({"group": group.get("group", ""), "pin_list": pins})
        if item["group_list"]:
            clean.append(item)
    return clean


def write_extraction_json(result: Any, output_path: str | Path) -> None:
    """以 UTF-8 和缩进格式写出提取结果或调试结果。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def build_extraction_summary(result: list[dict[str, Any]], pdf_name: str = "") -> dict[str, Any]:
    """统计封装数、引脚数、分组数及每组页码范围。"""
    packages = []
    total = 0
    for package in result:
        groups = []
        count = 0
        for group in package.get("group_list", []):
            pins = group.get("pin_list", [])
            pages = sorted({p.get("source_page") for p in pins if isinstance(p.get("source_page"), int)})
            count += len(pins)
            groups.append({"group": group.get("group", ""), "pin_count": len(pins), "page_start": pages[0] if pages else None, "page_end": pages[-1] if pages else None, "pages": pages})
        total += count
        packages.append({"pkg": package.get("pkg", ""), "pin_count": count, "table_count": len(groups), "group_list": groups})
    return {"pdf": pdf_name, "package_count": len(packages), "pin_count": total, "packages": packages}
