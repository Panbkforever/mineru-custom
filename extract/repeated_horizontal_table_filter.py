"""过滤横向重复的引脚字段块表格。

本模块只处理一种严格结构：同一张表从左到右重复出现至少两组完整的
``pin_no -> pin_name -> 可选 type`` 字段块，例如：

``Pin# | Pin Name | Type | Pin# | Pin Name | Type``

这类表通常只是把一份单封装引脚列表横向排版；项目中会从后续更完整的
Pin Descriptions 表提取，因此整张重复排版表直接排除，不送给模型，也不
进入单封装或多封装的逐行提取流程。

为了避免误删真正的多封装表，判断必须同时满足：

* 至少存在两个重复字段块；
* 每个字段块都从明确的 pin/ball/terminal 编号表头开始；
* 每个字段块都且只包含一个名称列，并允许包含一个 type 列；
* 所有字段块的字段顺序完全一致；
* 表头中不能夹杂 description、package 等其他字段。
"""

from __future__ import annotations

import re
from typing import Sequence


def is_repeated_horizontal_pin_block_table(headers: Sequence[str]) -> bool:
    """严格判断表头是否由多个完全相同的横向引脚字段块组成。"""

    roles = [_header_role(header) for header in headers]

    # 空表头或无法识别的列意味着结构并非纯重复字段块；此时不能过滤。
    if not roles or any(not role for role in roles):
        return False

    pin_positions = [index for index, role in enumerate(roles) if role == "pin_no"]
    if len(pin_positions) < 2 or pin_positions[0] != 0:
        return False

    blocks: list[tuple[str, ...]] = []
    for position_index, start in enumerate(pin_positions):
        end = (
            pin_positions[position_index + 1]
            if position_index + 1 < len(pin_positions)
            else len(roles)
        )
        block = tuple(roles[start:end])

        # 每组必须是 pin_no、pin_name，加上可选的一个 type；不接受缺列、
        # 重复名称列或顺序变化，防止误删多个封装共享名称列的表格。
        if block not in {
            ("pin_no", "pin_name"),
            ("pin_no", "pin_name", "type"),
        }:
            return False
        blocks.append(block)

    # 只有每组字段签名完全相同，才确认是横向重复排版。
    return len(set(blocks)) == 1


def _header_role(value: str) -> str:
    """将明确的引脚编号、名称和类型表头归一化为结构角色。"""

    text = _normalize_header(value)
    if re.search(r"\b(?:pin|ball|terminal)\s*(?:no|number)\b", text) or "pin#" in text:
        return "pin_no"
    if re.search(r"\b(?:pin|ball|signal|terminal)\s*name\b", text):
        return "pin_name"
    if text in {"type", "io", "i o", "i/o"} or re.search(
        r"\b(?:pin|signal|io|i o)\s*type\b", text
    ):
        return "type"
    return ""


def _normalize_header(value: str) -> str:
    """统一表头空白和标点，仅用于本模块的严格结构比较。"""

    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"[^a-z0-9#/]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()
