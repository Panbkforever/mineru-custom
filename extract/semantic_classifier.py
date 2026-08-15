"""用大模型判断候选表是否需要提取，以及需要提取哪些列。

模型层的职责严格限制为两项：

1. 每个请求接收最多四张初筛表的标题、完整表头和代表性数据行，逐表判断
   是否提取；小表的数据行保持完整。
2. 表格需要提取时，把目标列映射为 ``pin_no``、``pin_name`` 或 ``type``。
3. 每张表使用独立 ``request_id``；批内表格禁止相互合并或共享判断结果。

``classify_table_schema`` 不判断封装名、分组名或表格角色，也不生成最终引脚
记录。文档级封装总述表使用独立的 ``classify_package_catalog_table`` 请求和
返回协议，两种任务不能复用返回字段。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Sequence


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


def classify_table_schema(
    title: str,
    headers: list[str],
    table_rows: list[list[str]],
    header_paths: list[list[str]] | None = None,
    name_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """兼容单表调用；实际请求复用批量协议。"""
    request_id = "0"
    return classify_table_schema_batch(
        [
            {
                "request_id": request_id,
                "title": title,
                "headers": headers,
                "table_rows": table_rows,
                "header_paths": header_paths,
                "name_layout": name_layout,
            }
        ]
    )[request_id]


def classify_table_schema_batch(
    tables: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """一次判断最多四张完整候选表，并按 request_id 返回独立结果。"""
    if not tables:
        return {}
    if len(tables) > 4:
        raise ValueError("每个语义字段判断批次最多包含 4 张表")
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("启用语义字段判断需要先设置环境变量 DEEPSEEK_API_KEY")

    requests = []
    headers_by_id: dict[str, list[str]] = {}
    for index, table in enumerate(tables):
        request_id = str(table.get("request_id", index))
        if request_id in headers_by_id:
            raise ValueError(f"批次内 request_id 重复: {request_id}")
        headers = list(table.get("headers") or [])
        headers_by_id[request_id] = headers
        single_payload = build_schema_prompt_payload(
            title=str(table.get("title") or ""),
            headers=headers,
            table_rows=list(table.get("table_rows") or []),
            header_paths=table.get("header_paths"),
            name_layout=table.get("name_layout"),
            sampling=table.get("sampling"),
        )
        requests.append(
            {
                "request_id": request_id,
                "table": single_payload["table"],
            }
        )

    payload = {
        "task": (
            "Independently classify every candidate table. Return exactly one result "
            "for every request_id; never combine evidence or columns across tables."
        ),
        "rules": build_schema_prompt_payload("", [], [])["rules"],
        "allowed_fields": ["pin_no", "pin_name", "type"],
        "tables": requests,
        "output_schema": {
            "results": [
                {
                    "request_id": "copied exactly from input",
                    "should_extract": "boolean",
                    "columns": [
                        {
                            "column_index": 0,
                            "field": "pin_no|pin_name|type",
                        }
                    ],
                }
            ]
        },
    }
    response = call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You independently classify up to four complete semiconductor datasheet "
            "tables. Return valid JSON containing only results. Preserve every "
            "request_id exactly and never merge tables."
        ),
        max_tokens=int(os.getenv("DEEPSEEK_BATCH_MAX_TOKENS", "6000")),
    )
    return normalize_schema_batch_response(response, headers_by_id)


def classify_package_catalog_table(
    table: Any,
    source_name: str = "",
    target_tables: Sequence[Any] = (),
) -> dict[str, Any]:
    """兼容单表调用；实际请求复用批量协议。

    ``table`` 使用鸭子类型，避免语义模块反向依赖封装目录的数据类。
    """
    request_id = str(getattr(table, "table_id", 0))
    return classify_package_catalog_tables(
        [(request_id, table)],
        source_name=source_name,
        target_tables=target_tables,
    )[request_id]


def classify_package_catalog_tables(
    tables: Sequence[tuple[str, Any]],
    *,
    source_name: str = "",
    target_tables: Sequence[Any] = (),
) -> dict[str, dict[str, Any]]:
    """一次判断最多四张封装总述候选表，结果按 request_id 隔离。"""
    if not tables:
        return {}
    if len(tables) > 4:
        raise ValueError("每个封装目录判断批次最多包含 4 张表")

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("启用封装目录判断需要先设置环境变量 DEEPSEEK_API_KEY")

    payload = {
        "task": (
            "Independently classify every complete candidate table for document-level "
            "package resolution. Return one result per request_id. Never combine tables "
            "or return package names and extracted cell values."
        ),
        "rules": [
            "Read the complete table, title and chapter context.",
            "Treat tables titled Device Information, Package Information, Packaging Information, Ordering Information, 器件信息, 封装信息, 包装信息 or 订购信息 as the strongest catalog candidates, but still classify their actual structure instead of accepting by title alone.",
            "Use target pin-table titles only as context for deciding which candidate column contains the device identity used for cross-table association.",
            "identity_summary means each data row describes one independent physical pinout slot. A row may contain a device identity such as INA290 and a physical package type such as SC-70.",
            "packaging_metadata means rows contain orderable SKUs, physical package type, package drawing, pin count or shipment variants.",
            "An ordering or packaging table must never be identity_summary merely because its SKU contains a device name.",
            "package_identity is a device/model identifier used only to associate tables, for example INA290, INA2290 or INA4290. It is never the public package name.",
            "package_type is the public physical package family such as SC-70, VSSOP or QFN.",
            "package_drawing is a drawing/code such as DCK, DGK or RGV; it distinguishes physical package variants but is not the public package name.",
            "orderable_sku is a purchasable ordering string with grade, temperature or shipment suffixes; it is not package_identity.",
            "pin_count is the number of physical pins or balls.",
            "header_row_index is zero-based and points to the last header row; data starts on the following row.",
            "Return irrelevant when the table neither establishes identities nor supplies packaging metadata.",
            "This task does not identify pin_no, pin_name, type, group names or row values.",
        ],
        "document": {"source_name": source_name},
        "tables": [
            {
                "request_id": request_id,
                "table_id": getattr(table, "table_id", None),
                "page": (
                    getattr(table, "page_idx", None) + 1
                    if isinstance(getattr(table, "page_idx", None), int)
                    else None
                ),
                "title": getattr(table, "title", ""),
                "group_context": getattr(table, "group_context", ""),
                "current_chapter_titles": list(
                    getattr(table, "current_chapter_titles", ()) or ()
                ),
                "headers": list(getattr(table, "headers", ()) or ()),
                "rows": [
                    list(row)
                    for row in (getattr(table, "rows", ()) or ())
                ],
            }
            for request_id, table in tables
        ],
        "target_pin_tables": [
            {
                "table_id": getattr(target, "table_id", None),
                "title": getattr(target, "title", ""),
                "current_chapter_titles": list(
                    getattr(target, "current_chapter_titles", ()) or ()
                ),
                "headers": list(getattr(target, "headers", ()) or ()),
            }
            for target in target_tables
        ],
        "output_schema": {"results": [{
            "request_id": "copied exactly from input",
            "is_package_summary": "boolean",
            "table_role": "identity_summary|packaging_metadata|irrelevant",
            "header_row_index": "zero-based integer",
            "columns": [{
                "column_index": "zero-based integer",
                "role": (
                    "package_identity|package_type|package_drawing|"
                    "pin_count|orderable_sku|ignore"
                ),
            }],
        }]},
    }
    response = call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You independently classify up to four complete semiconductor package-summary "
            "or packaging tables. Return valid JSON containing only results. Preserve "
            "every request_id and never merge tables or output package names/cell values."
        ),
        max_tokens=int(os.getenv("EXTRACT_PACKAGE_BATCH_MAX_TOKENS", "7000")),
        timeout=float(os.getenv("EXTRACT_PACKAGE_TIMEOUT", "60")),
    )
    return normalize_package_catalog_batch_response(
        response,
        {request_id for request_id, _ in tables},
    )


def build_schema_prompt_payload(
    title: str,
    headers: list[str],
    table_rows: list[list[str]],
    header_paths: list[list[str]] | None = None,
    name_layout: dict[str, Any] | None = None,
    sampling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造第一次模型调用请求；表头完整，超长数据区由上游采样。"""

    return {
        "task": (
            "Decide whether this semiconductor datasheet table should be used to extract "
            "physical pin/ball/terminal records. If it should, identify only the columns "
            "needed as pin_no, pin_name, or type."
        ),
        "rules": [
            "Use semantic meaning, not exact header names only.",
            "Return should_extract=true only when the table has at least one column containing physical pin, ball, or terminal identifiers.",
            "If there is no physical pin_no, ball-number, or terminal-number column, return should_extract=false even when the table contains logical Pin Name, Signal Name, Type, Mode, or Function columns.",
            "A pin_name column is optional. A physical-number-only table may still be extracted because missing names are filled as Reserved by deterministic code.",
            "If different packages have separate physical pin/ball number columns, return every one of those columns as pin_no; do not select only one package.",
            "BALL NAME, SIGNAL NAME, PIN NAME and TERMINAL NAME are equivalent pin_name semantics in this project.",
            "Read table.name_layout before selecting name columns. When mode=equivalent_names, return only one complete pin_name column.",
            "When mode=package_branches, return one pin_name column for every listed branch; never collapse multiple branches into one column.",
            "When mode=parallel_name_branches, also return one pin_name column for every listed operating-mode branch; these branches are not packages.",
            "Rows may be a deterministic sample from a large table. Use them only to validate column semantics; never assume omitted rows or packages do not exist.",
            "Structural branch labels identify parallel object/package branches, but they are not final public package names.",
            "For multiple type-like columns, select only the type most directly describing the pin/signal, such as SIGNAL TYPE rather than BUFFER TYPE.",
            "Do not select description, conditions, min/typ/max, unit, reset state, power source, notes, ordering, or other auxiliary columns.",
            "Do not return package names, group names, table roles, confidence scores, reasons, or extracted row values.",
        ],
        "allowed_fields": ["pin_no", "pin_name", "type"],
        "table": {
            "title": title,
            "headers": headers,
            # header_paths 是 span-aware 解析得到的完整父子表头，不由模型生成。
            "header_paths": header_paths or [],
            # name_layout 只提示名称列结构；模型仍只返回是否提取和列映射。
            "name_layout": name_layout or {},
            # 上游保证完整保留全部多层表头；超长表只压缩正式数据区。
            "sampling": sampling or {
                "strategy": "caller_provided",
                "total_data_rows": len(table_rows),
                "sampled_data_rows": len(table_rows),
            },
            "rows": table_rows,
        },
        "output_schema": {
            "should_extract": "boolean",
            "columns": [
                {
                    "column_index": 0,
                    "field": "pin_no|pin_name|type",
                }
            ],
        },
    }


def call_deepseek_json(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """调用唯一的字段判断模型并返回 JSON。"""

    return call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You only decide whether one complete semiconductor datasheet table should "
            "be extracted and map its needed columns. Return valid JSON containing only "
            "should_extract and columns. Do not return any other fields or final pin records."
        ),
        max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "3000")),
    )


def call_model_json(
    payload: dict[str, Any],
    *,
    api_key: str,
    system_prompt: str,
    max_tokens: int,
    timeout: float | None = None,
) -> dict[str, Any]:
    """调用 OpenAI 兼容 JSON 接口；业务协议由调用方的 prompt 决定。"""

    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    request_timeout = (
        float(timeout)
        if timeout is not None
        else float(os.getenv("DEEPSEEK_TIMEOUT", "90"))
    )
    url = f"{base_url}/chat/completions"

    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }

    last_error: Exception | None = None
    max_retries = max(1, int(os.getenv("DEEPSEEK_MAX_RETRIES", "4")))
    retry_base = float(os.getenv("DEEPSEEK_RETRY_BASE_SECONDS", "2"))
    for attempt in range(max_retries):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"DeepSeek API 请求失败: HTTP {exc.code} {detail}")
            if exc.code == 429 and attempt < max_retries - 1:
                time.sleep(retry_base * (2**attempt))
                continue
            raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"DeepSeek API 请求失败: {exc}")
            if attempt < max_retries - 1:
                time.sleep(retry_base * (2**attempt))
                continue
            raise last_error from exc
        except TimeoutError as exc:
            last_error = RuntimeError(f"DeepSeek API 请求超时: {exc}")
            if attempt < max_retries - 1:
                time.sleep(retry_base * (2**attempt))
                continue
            raise last_error from exc

        completion = json.loads(raw)
        content = str(completion["choices"][0]["message"].get("content") or "")
        try:
            return parse_json_content(content)
        except ValueError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(retry_base)
                continue
            break
    raise last_error or ValueError("DeepSeek returned invalid JSON")


def parse_json_content(content: str) -> dict[str, Any]:
    """Parse JSON even when the model wraps it in markdown or extra text."""
    content = content.strip()
    if not content:
        raise ValueError("DeepSeek returned empty message content")

    candidates = [content]
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.insert(0, fenced_match.group(1))

    object_match = re.search(r"\{.*\}", content, re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0))

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed

    preview = content[:300].replace("\n", "\\n")
    raise ValueError(f"DeepSeek returned non-JSON content: {preview}") from last_error


def normalize_schema_response(response: dict[str, Any], headers: list[str]) -> dict[str, Any]:
    """只保留约定的两个返回字段，并过滤越界或非法的列映射。"""

    columns = []
    seen: set[tuple[int, str]] = set()
    for item in response.get("columns") or []:
        try:
            index = int(item.get("column_index"))
        except (TypeError, ValueError, AttributeError):
            continue
        if index < 0 or index >= max(1, len(headers)):
            continue
        field = normalize_schema_field(str(item.get("field") or "ignore"))
        if field == "ignore":
            continue
        key = (index, field)
        if key in seen:
            continue
        seen.add(key)
        columns.append(
            {
                "column_index": index,
                "header": headers[index] if index < len(headers) else f"column_{index + 1}",
                "field": field,
            }
        )

    return {
        "should_extract": bool(response.get("should_extract")),
        "columns": columns,
    }


def normalize_schema_batch_response(
    response: dict[str, Any],
    headers_by_id: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """校验批量字段判断结果，缺失或重复的 request_id 不做猜测。"""

    normalized: dict[str, dict[str, Any]] = {}
    for item in response.get("results") or []:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("request_id", ""))
        if request_id not in headers_by_id or request_id in normalized:
            continue
        normalized[request_id] = normalize_schema_response(
            item,
            headers_by_id[request_id],
        )
    return normalized


def normalize_package_catalog_response(response: dict[str, Any]) -> dict[str, Any]:
    """只保留表格结构协议，彻底丢弃模型生成的名称和值。"""

    table_role = re.sub(
        r"[^a-z_]+",
        "",
        str(response.get("table_role") or "").lower(),
    )
    if table_role not in {
        "identity_summary",
        "packaging_metadata",
        "irrelevant",
    }:
        table_role = "irrelevant"
    try:
        header_row_index = max(0, int(response.get("header_row_index", 0)))
    except (TypeError, ValueError):
        header_row_index = 0

    allowed_roles = {
        "package_identity",
        "package_type",
        "package_drawing",
        "pin_count",
        "orderable_sku",
        "ignore",
    }
    columns = []
    seen = set()
    for item in response.get("columns") or []:
        if not isinstance(item, dict):
            continue
        try:
            column_index = int(item.get("column_index"))
        except (TypeError, ValueError):
            continue
        role = re.sub(
            r"[^a-z_]+",
            "",
            str(item.get("role") or "").lower(),
        )
        if column_index < 0 or role not in allowed_roles:
            continue
        key = (column_index, role)
        if key in seen:
            continue
        seen.add(key)
        columns.append(
            {
                "column_index": column_index,
                "role": role,
            }
        )
    return {
        "is_package_summary": bool(response.get("is_package_summary")),
        "table_role": table_role,
        "header_row_index": header_row_index,
        "columns": columns,
    }


def normalize_package_catalog_batch_response(
    response: dict[str, Any],
    expected_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """校验封装目录批量结果，严格按 request_id 关联原表。"""

    normalized: dict[str, dict[str, Any]] = {}
    for item in response.get("results") or []:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("request_id", ""))
        if request_id not in expected_ids or request_id in normalized:
            continue
        normalized[request_id] = normalize_package_catalog_response(item)
    return normalized


def normalize_schema_field(field: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    aliases = {
        "ball_no": "pin_no",
        "ball_number": "pin_no",
        "terminal_no": "pin_no",
        "terminal_number": "pin_no",
        "pin_number": "pin_no",
        "signal_no": "pin_no",
        "signal_number": "pin_no",
        "ball_name": "pin_name",
        "signal_name": "pin_name",
        "terminal_name": "pin_name",
        "pin_signal_name": "pin_name",
        "signal_type": "type",
        "pin_type": "type",
        "io_type": "type",
        "i_o": "type",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"pin_no", "pin_name", "type"}:
        return "ignore"
    return normalized
