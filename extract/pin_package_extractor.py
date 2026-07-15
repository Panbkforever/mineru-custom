"""从 MinerU 的表格结果中提取器件引脚和封装信息。

本文件只做一件事：把表格转换为项目约定的 JSON 结构。

处理流程固定为五个阶段，阶段之间不互相调用：

1. 表格判断：判断当前表是不是“物理引脚/封装关系表”。
2. 字段判断：只判断每一列的语义，不读取行来生成输出记录。
3. 多封装分析：为真正的多封装表生成 package 与列/行的绑定计划。
4. 行提取：单封装和多封装走各自独立逻辑，完整读取已经绑定的数据行。
5. 结果整理：拆分显式 pin_no 分隔符、清洗 pin_name、分组和合并封装别名。

特别重要的项目规则：

* 一列一旦被判定为需要字段，就不因为某一行为空而跳过该行。
* pin_no 按原文中的空格、逗号、斜杠等显式分隔符拆分。
* 对同一字母前缀且数字递增的范围进行展开，例如 A1-A5 展开为 A1、A2、A3、A4、A5；
  前后字母不同的 A1-C3 不展开，数字倒序的 A5-A1 也不展开。
* 对 BGA 行列编号的方括号范围进行展开，例如 L[7:12] 展开为 L7 至 L12；
  此处输入已经由字段判断确定为 pin_no，因此不限制字母前缀长度。
* pin_no 和 pin_name 的多个值按位置对应；只有两列拆分后的数量完全一致时才同步拆分。
  数量不一致时保留原 pin_name，不强行猜测对应关系。
* 当前只对 pin_no 和 pin_name 做跨值同步拆分，不拆 type、description 等其他字段。
* pin_name 为空填 ``Reserved``；去掉末尾的 ``(数字)`` 和 ``(continued)``。
* 同一个 pin_no 出现多次时不合并记录；不同 type 也不合并。
* 多个 type 列同时存在时，只保留最接近 signal/pin 语义的一个，优先 SIGNAL TYPE、PIN TYPE、I/O TYPE。
* “Pin Configuration and Function” 这类坐标矩阵不是物理引脚表，表级直接排除。
* 开启语义判断时，模型接收初筛后的表格标题、表头和完整表格；模型只返回
  ``should_extract`` 以及 ``pin_no``、``pin_name``、``type`` 的列映射。
* 表格分组标题只按行首 ``Table xxx``/``表 xxx`` 识别，不要求标题中必须
  含有 Pin、Signal 等关键词；清理编号和 ``(continued)`` 后作为 group 名。
* 初筛后先调用 ``special_table_handlers.py``。特殊表只有完整命中专用规则才
  绕过模型；当前 Reserved/NC 表会直接保留真实 Reserved 行并排除不存在位置。
* 通过表格/字段判断后调用 ``multi_package_extractor.py``。多个封装专属
  pin_no 列、package 控制列和 package 分段行走多封装分支；横向重复的
  Pin#/Pin Name/Type 字段块只是单封装排版，不按多封装处理。
* 语义字段判断默认并发数为 4，可通过 ``EXTRACT_SCHEMA_WORKERS`` 覆盖。
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from extract.table_association_rules import (
    PackageSnapshot,
    resolve_table_package_association,
)
from extract.special_table_handlers import find_special_table_match
from extract.multi_package_extractor import (
    BoundPackageRow,
    MultiPackagePlan,
    analyze_multi_package_table,
    iter_bound_package_rows,
    plan_to_debug,
)


# ---------------------------------------------------------------------------
# 数据结构：判断阶段的结果和输出阶段的结果分开
# ---------------------------------------------------------------------------


@dataclass
class TableCandidate:
    html: str
    page_idx: int | None
    title: str = ""


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
    pkg: str = ""
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


@dataclass(frozen=True)
class PackageIdentity:
    display: str
    key: str
    pin_count: str = ""
    family: str = ""
    code: str = ""


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
    # 转成列表，保证后面可以重复访问候选表；packages 保存最终分组结果。
    candidates = list(tables)
    packages: dict[str, dict[str, Any]] = {}

    # 第一阶段：只准备候选表，不创建任何 pin 记录。
    prepared = []
    for table_id, table in enumerate(candidates):
        # 一个候选表对应一个二维字符串数组，后续判断和提取都基于它。
        rows = parse_html_table(table.html)
        debug = {
            "table_id": table_id,
            "source": source_name,
            "page": table.page_idx + 1 if isinstance(table.page_idx, int) else None,
            "title": table.title,
            "row_count": len(rows),
            "status": "pending",
            "skip_reason": "",
        }
        LAST_EXTRACTION_DEBUG.append(debug)

        if len(rows) < 2:
            skip(debug, "too_few_rows")
            continue
        if is_pinout_matrix_table(rows, table.title):
            skip(debug, "pinout_matrix_table")
            continue

        header_index, headers = choose_header_row(rows, table.title, semantic=use_semantic_classifier)
        if header_index < 0:
            skip(debug, "no_candidate_header")
            continue
        data_rows = rows[header_index + 1 :]
        if is_ordering_table(headers):
            skip(debug, "ordering_table")
            continue
        if not is_loose_candidate(table.title, headers, data_rows):
            skip(debug, "not_pin_table_candidate")
            continue
        if is_non_physical_port_function_table(table.title, headers):
            skip(debug, "non_physical_port_function_table")
            continue

        rule_columns = classify_columns(headers, data_rows, table.title)
        prepared.append(
            {
                "table_id": table_id,
                "table": table,
                "rows": rows,
                "header_index": header_index,
                "headers": headers,
                "data_rows": data_rows,
                "rule_columns": rule_columns,
                "debug": debug,
            }
        )
        debug["headers"] = headers
        debug["rule_columns"] = decisions_to_debug(rule_columns)

    # 第二阶段：先完成所有表级和字段级判断，再进入任何行提取。
    # 此处一次性完成全部表格判断，避免边判断边产出 pin 记录。
    decisions = decide_all_tables(prepared, use_semantic_classifier, include_debug)

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
        )
        multi_package_plans[item["table_id"]] = plan
        item["debug"]["multi_package_plan"] = plan_to_debug(plan)

    # 第四阶段：只有表格判断和多封装分析都结束后，才允许创建 pin 记录。
    for item in prepared:
        table_decision = decisions[item["table_id"]]
        debug = item["debug"]
        debug["decision"] = decision_to_debug(table_decision)
        if not table_decision.should_extract:
            skip(debug, table_decision.reason or "table_rejected")
            continue

        association = resolve_table_package_association(
            item["table"].title,
            item["headers"],
            item["data_rows"],
            table_decision.columns,
            build_package_snapshots(packages),
        )
        default_pkg = (
            table_decision.pkg
            or association.package
            or infer_package_name(item["table"].title)
        )
        group_name = clean_group_name(
            table_decision.group
            or infer_group_name(item["table"].title)
            or infer_group_name_from_headers(item["headers"], default_pkg)
            or "Pin/Package Table"
        )

        plan = multi_package_plans[item["table_id"]]
        current_group_name = group_name
        extracted_count = 0

        # 真正的多封装表严格执行绑定计划。每个封装独立读取自己的 pin_no，
        # 不能再进入普通逻辑把多个编号列拼接成一个字段。
        if plan.is_multi_package:
            for bound_row in iter_bound_package_rows(plan, item["data_rows"]):
                for record in extract_records_from_bound_package_row(bound_row):
                    record_pkg = record.pop("_pkg")
                    if include_debug and source_name:
                        record["source"] = source_name
                    if include_debug and item["table"].page_idx is not None:
                        record["source_page"] = item["table"].page_idx + 1
                    identity = build_package_identity(record_pkg)
                    bucket = get_package_bucket(packages, identity)
                    group = get_or_create_group(bucket, current_group_name)
                    add_pin_record_to_group(group, record)
                    extracted_count += 1

        # 单封装表保留原有逐行提取逻辑，不经过多封装绑定。
        else:
            for row_index, row in enumerate(item["data_rows"]):
                if (
                    table_decision.included_row_indexes is not None
                    and row_index not in table_decision.included_row_indexes
                ):
                    continue
                if is_group_row(row):
                    current_group_name = clean_group_name(first_non_empty(row)) or current_group_name
                    continue
                for record in extract_records_from_row(row, table_decision.columns):
                    record_pkg = record.pop("_pkg", default_pkg)
                    record.pop("_raw_fields", None)
                    if include_debug and source_name:
                        record["source"] = source_name
                    if include_debug and item["table"].page_idx is not None:
                        record["source_page"] = item["table"].page_idx + 1
                    identity = build_package_identity(record_pkg)
                    bucket = get_package_bucket(packages, identity)
                    group = get_or_create_group(bucket, current_group_name)
                    add_pin_record_to_group(group, record)
                    extracted_count += 1

        if extracted_count:
            debug.update({
                "status": "extracted",
                "pin_count": extracted_count,
                "pkg": (
                    " | ".join(binding.package for binding in plan.bindings)
                    if plan.is_multi_package
                    else default_pkg
                ),
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
            should_extract=True,
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
        return TableDecision(False, reason="missing_pin_name_or_number_column", columns=columns)
    return TableDecision(
        True,
        table_role="rule_pin_table",
        pkg=infer_package_name(item["table"].title),
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


def has_required_columns(columns: list[ColumnDecision]) -> bool:
    """检查字段判断结果是否同时包含编号列和名称列。"""
    fields = {normalize_field_name(c.field_name) for c in columns}
    return bool(fields & {"pin_no", "package_pin_no"}) and bool(fields & {"pin_name"})


def is_pin_package_table(columns: list[ColumnDecision]) -> bool:
    """检查字段映射是否具有真实引脚表的最小结构。"""
    if not has_required_columns(columns):
        return False
    number = [c for c in columns if normalize_field_name(c.field_name) in {"pin_no", "package_pin_no"}]
    name = [c for c in columns if normalize_field_name(c.field_name) == "pin_name"]
    return any(classify_header(c.raw_header)[1] >= 3 for c in number) and any(classify_header(c.raw_header)[1] >= 3 for c in name)


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
    if any(word in h for word in ("pin no", "pin number", "ball no", "ball number", "terminal no", "terminal number", "引脚编号", "端子编号")):
        return "pin_no", 5
    # Reserved/NC 表常把物理编号列简写为 PINS、BALLS 或 TERMINALS。
    if h in {"pins", "balls", "terminals"}:
        return "pin_no", 4
    if any(word in h for word in ("pin name", "ball name", "signal name", "terminal name", "引脚名称", "信号名称", "引脚名")):
        return "pin_name", 5
    if any(word in h for word in ("signal type", "pin type", "terminal type", "io type", "i o type", "引脚类型", "信号类型")):
        return "type", 5
    if h in {"no", "no.", "number", "signal no", "signal no."}:
        return "pin_no", 4
    if h in {"signal", "name"}:
        return "pin_name", 3
    if h in {"type", "io", "i o", "i/o"} or h.endswith(" type"):
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

    pin_numbers = split_pin_numbers(bound_row.pin_no)
    pin_names = split_parallel_pin_names(bound_row.pin_name, len(pin_numbers))
    records = []
    for index, pin_no in enumerate(pin_numbers):
        record: dict[str, Any] = {
            "pin_no": pin_no,
            "pin_name": pin_names[index] if pin_names else bound_row.pin_name,
            "_pkg": bound_row.package,
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

    # fields 是标准字段值；package_columns 单独保存“封装专属 pin 列”。
    fields: dict[str, str] = {}
    package_columns: list[tuple[str, str]] = []
    # raw_fields 仅用于 debug，不参与最终字段判断和输出。
    raw_fields: dict[str, str] = {}
    for column in columns:
        value = row[column.index].strip() if column.index < len(row) else ""
        raw_fields[column.raw_header or f"column_{column.index + 1}"] = value
        field_name = normalize_field_name(column.field_name)
        if column.field_name == "package_pin_no":
            package_columns.append((column.raw_header, value))
        elif field_name:
            # 同一个字段有多个列时保留原始信息，不在这里合并 pin 记录。
            fields[field_name] = merge_field_value(fields.get(field_name, ""), value)

    records = []
    if package_columns:
        # 封装专属编号列优先：每个封装列分别生成自己的 pin 记录。
        for package_header, value in package_columns:
            pin_numbers = split_pin_numbers(value)
            pin_names = split_parallel_pin_names(fields.get("pin_name", ""), len(pin_numbers))
            for index, pin_no in enumerate(pin_numbers):
                record = dict(fields)
                record["pin_no"] = pin_no
                if pin_names:
                    # 只有数量完全对应时，才覆盖整行共享的 pin_name。
                    record["pin_name"] = pin_names[index]
                record["_pkg"] = clean_package_label(package_header)
                record["_raw_fields"] = raw_fields
                records.append(record)
        if records:
            return records

    pin_value = fields.get("pin_no", "")
    pin_numbers = split_pin_numbers(pin_value)
    pin_names = split_parallel_pin_names(fields.get("pin_name", ""), len(pin_numbers))
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


def split_parallel_pin_names(value: str, expected_count: int) -> list[str]:
    """按 pin_no 数量拆分同一行中的 pin_name。

    只处理 pin_name 自身的显式逗号、分号或斜杠分隔。普通空格不作为
    分隔符，因为名称可能是 ``Power Supply`` 这样的正常短语。只有拆分
    后的数量与 pin_no 数量完全相同，才认为两列可以按位置对应。
    """

    value = plain_text(str(value or "")).strip()
    if expected_count <= 1 or not value:
        return []
    # 名称中的普通空格不能作为分隔符，例如 Power Supply。
    parts = [part.strip() for part in re.split(r"[,，;；/／]+", value) if part.strip()]
    if len(parts) == expected_count:
        # 数量相等意味着可以和 pin_no 按索引建立一一对应关系。
        return parts
    # 数量不等时不猜测，调用方会把原名称复制到各个 pin 上。
    return []


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
# 表格读取、标题、分组和封装归并
# ---------------------------------------------------------------------------


TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
BALL_TOKEN_RE = re.compile(r"\b[A-Z]{1,2}\d{1,3}\b")
PACKAGE_RE = re.compile(r"\b(?:\d{2,4}\s*)?(?:qfp|lqfp|tqfp|qfn|bga|soic|sop|pdip|dfn|wqfn|zce\d*[a-z]*|nzn\d*[a-z]*|zwt|zjz|zay|rgz|rha|rhb|pz|pm|pw)\b", re.IGNORECASE)
TYPE_VALUES = {"i", "o", "io", "i/o", "i/o/z", "oz", "od", "odz", "p", "ipu", "ipd", "analog", "digital", "power", "ground", "gnd", "supply", "input", "output"}


def parse_html_table(html: str) -> list[list[str]]:
    """把一个 HTML table 转成按行、按列排列的纯文本二维数组。"""
    rows = []
    for tr in TR_RE.finditer(html):
        row = [plain_text(match.group(2)) for match in CELL_RE.finditer(tr.group(2))]
        if row:
            rows.append(row)
    return rows


def choose_header_row(rows: list[list[str]], title: str = "", semantic: bool = False) -> tuple[int, list[str]]:
    """在表格前几行中选择最像字段表头的一行。"""
    best = (-1, -1, [])
    for index in range(min(6, len(rows))):
        headers = build_combined_headers(rows, index)
        score = sum(classify_header(header)[1] for header in headers)
        if score > best[1]:
            best = (index, score, headers)
    if best[1] < (2 if semantic else 4):
        return -1, []
    return best[0], best[2]


def build_combined_headers(rows: list[list[str]], header_index: int) -> list[str]:
    """合并多层表头，使跨行表头在字段判断时成为一个完整文字。"""
    width = max((len(row) for row in rows[: header_index + 1]), default=0)
    result = []
    for index in range(width):
        parts = []
        for row in rows[: header_index + 1]:
            if index < len(row) and row[index].strip() and row[index].strip() not in parts:
                parts.append(row[index].strip())
        result.append(" ".join(parts))
    return result


def is_group_row(row: list[str]) -> bool:
    """识别只有一个重复文本的分组标题行，而不是普通数据行。"""
    values = [cell.strip() for cell in row if cell.strip()]
    return bool(values) and (len(values) == 1 or len(set(values)) == 1) and not looks_like_pin_list(values[0])


def first_non_empty(row: list[str]) -> str:
    """返回一行中第一个非空单元格，用于读取分组标题。"""
    return next((cell.strip() for cell in row if cell.strip()), "")


def infer_group_name(text: str) -> str:
    """清理已经绑定到候选表的标题，不再检查标题包含哪些语义词。"""
    return clean_group_name(text)


def extract_table_title(text: str) -> str:
    """从独立文本行中识别 ``Table xxx``/``表 xxx`` 标题。

    这里只判断标题格式，不判断后面的标题内容。这样 ``Signal Descriptions``、
    ``Pin Attributes`` 或其他名称都能被保留，同时普通正文不会更新当前标题。
    """

    text = re.sub(r"\s+", " ", plain_text(text)).strip()
    if not text:
        return ""
    match = re.match(
        r"^(?:Table|表格?|表)\s*[\w一二三四五六七八九十百千万]+(?:[.\-][\w一二三四五六七八九十百千万]+)*\s*[.:：\-–—]?\s*.+$",
        text,
        flags=re.IGNORECASE,
    )
    return clean_group_name(text) if match else ""


def infer_group_name_from_headers(headers: list[str], package: str = "") -> str:
    """当附近没有标题时，根据表头组合生成保守的组名。"""
    h = normalize_header(" ".join(headers))
    suffix = f", {package}" if package else ""
    if "connection requirements" in h or "connectivity requirements" in h:
        return f"Connectivity Requirements{suffix}"
    if "description" in h and ("function" in h or "signal type" in h):
        return f"Signal Descriptions{suffix}"
    if "pin" in h or "ball" in h or "terminal" in h:
        return f"Pin Attributes{suffix}"
    return ""


def clean_group_name(value: str) -> str:
    """去掉 Table 编号和 continued 标记，保留组的语义名称。"""
    value = re.sub(r"\s+", " ", plain_text(value)).strip()
    value = re.sub(r"\s*[（(]\s*continued\s*[）)]", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:table|表格?|表)\s*[\w.\-一二三四五六七八九十百千万]+\s*[:：.\-–—]?\s*", "", value, flags=re.IGNORECASE)
    return value or ""


def infer_package_name(text: str) -> str:
    """从标题中的“XXX Package”结构提取初始封装名。"""
    text = plain_text(text)
    match = re.search(r"\b([A-Z0-9][A-Z0-9-]{1,20})\s+Package\b", text, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def build_package_identity(label: str) -> PackageIdentity:
    """把显示名称转换为用于分组的稳定封装身份。"""
    display = clean_package_label(label)
    if looks_like_pin_list(display):
        display = ""
    compact = normalize_header(display)
    code = ""
    match = PACKAGE_RE.search(display)
    if match:
        code = match.group(0).upper().replace(" ", "")
    key = f"code={code}" if code else f"label={compact or 'unknown'}"
    return PackageIdentity(display, key, code=code)


def get_package_bucket(packages: dict[str, dict[str, Any]], identity: PackageIdentity) -> dict[str, Any]:
    """按封装 key 获取或创建输出桶，并追加新出现的封装别名。"""
    key = identity.key
    if key not in packages:
        packages[key] = {"pkg": "", "pkg_key": key, "_groups": {}, "_aliases": [], "_identity": identity}
    bucket = packages[key]
    if identity.display and identity.display not in bucket["_aliases"]:
        bucket["_aliases"].append(identity.display)
        bucket["pkg"] = " | ".join(bucket["_aliases"])
    return bucket


def build_package_snapshots(packages: dict[str, dict[str, Any]]) -> list[PackageSnapshot]:
    """把已提取结果压缩成关联规则需要的已知封装快照。"""
    snapshots = []
    for bucket in packages.values():
        pins, names = set(), set()
        for group in bucket["_groups"].values():
            for pin in group.pin_list:
                pins.add(pin.get("pin_no", ""))
                names.add(pin.get("pin_name", "").upper())
        snapshots.append(PackageSnapshot(bucket.get("pkg", ""), pins, names))
    return snapshots


def get_or_create_group(bucket: dict[str, Any], name: str) -> ExtractedGroup:
    """在封装桶中获取或创建一个表格/小分组。"""
    name = clean_group_name(name) or "Pin/Package Table"
    if name not in bucket["_groups"]:
        bucket["_groups"][name] = ExtractedGroup(name)
    return bucket["_groups"][name]


def build_public_result(packages: dict[str, dict[str, Any]], include_debug: bool) -> list[dict[str, Any]]:
    """把内部 bucket 结构转换成项目约定的公开 JSON 结构。"""
    result = []
    for bucket in packages.values():
        groups = [{"group": group.group, "pin_list": group.pin_list} for group in bucket["_groups"].values() if group.pin_list]
        if groups:
            item = {"pkg": bucket["pkg"], "group_list": groups}
            if include_debug:
                item["pkg_key"] = bucket["pkg_key"]
            result.append(item)
    return result


def clean_package_label(value: str) -> str:
    """清理封装表头中的脚注、Package 后缀和多余空格。"""
    value = plain_text(value)
    value = re.sub(r"\[[^]]+\]", "", value)
    value = re.sub(r"\bpackage\b", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip(" -:/")


# ---------------------------------------------------------------------------
# 候选表来源
# ---------------------------------------------------------------------------


def iter_table_candidates(middle_json: dict[str, Any]) -> list[TableCandidate]:
    """按页面阅读顺序从 middle_json 收集 HTML 表格及附近标题。"""
    result = []
    current_title = ""
    for page in middle_json.get("pdf_info", []):
        page_idx = page.get("page_idx") if isinstance(page, dict) else None
        recent = []
        for span in iter_spans_in_reading_order(page):
            html = span.get("html") if isinstance(span, dict) else None
            text = plain_text((span.get("content") or span.get("text") or "") if isinstance(span, dict) else "")
            if isinstance(html, str) and "<table" in html.lower():
                result.append(TableCandidate(html, page_idx if isinstance(page_idx, int) else None, current_title or " ".join(recent[-5:])))
            elif text:
                # 只有明确的 Table xxx 标题才能替换当前表标题。
                current_title = extract_table_title(text) or current_title
                recent.append(text)
                recent = recent[-10:]
    return result


def iter_table_candidates_from_markdown(markdown: str) -> list[TableCandidate]:
    """从最终 Markdown 中收集 HTML 表格，并绑定最近的标题文本。"""
    result = []
    table_re = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
    cursor = 0
    last_title = ""
    for match in table_re.finditer(markdown):
        before = markdown[cursor : match.start()]
        texts = [plain_text(line).strip() for line in before.splitlines() if plain_text(line).strip()]
        for text in texts:
            # 标题内容不设 Pin/Signal 等关键词限制，只检查 Table xxx 格式。
            last_title = extract_table_title(text) or last_title
        result.append(TableCandidate(match.group(0), None, last_title or " ".join(texts[-5:])))
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
        "pkg": decision.pkg,
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
