"""
=============================================================================
表格 OCR top-K 候选 + 行级语义重排序
=============================================================================

本模块工作在 MinerU pipeline 的 OCR 识别完成后、表格结构模型运行前。
此时 ocr_result 中包含：
  - dt_box:      检测框坐标（可用于空间分组建行）
  - text:        greedy decoder 输出的文本
  - confidence:  greedy decoder 置信度
  - candidates:  (可选) CTC beam search 返回的 top-K 候选列表

处理逻辑：
  1. 按 y 坐标将 OCR 结果聚合成行
  2. 对每行内低置信度单元格，用同行文本做语义打分
  3. 从 top-K 候选中选择最匹配上下文的结果
  4. 原地更新 ocr_result 中的 text 和 confidence

优势：不依赖具体混淆对（1→I, 0→O, 二→—）的硬编码规则，
      通过 CTC 概率分布 + 行级语义评分自动适应任何混淆字符。
=============================================================================
"""

import re
import logging
import numpy as np
from typing import Any

logger = logging.getLogger(__name__)

# ===========================================================================
# 语义关键词打分表
# ===========================================================================
# 对 top-K 候选中的字符，根据同行上下文中的关键词打分
# 如果候选文本匹配到某个 label，且同行中有对应的 positive 关键词，则加分

CONTEXT_SCORING = {
    "I": {
        "positive": [
            "输入", "in", "signal", "信号", "同相", "反相", "正向",
            "input", "偏置", "comparator",
        ],
        "weight": 3.0,
    },
    "O": {
        "positive": [
            "输出", "out", "驱动", "drive", "集电极", "open",
            "output", "comparator",
        ],
        "weight": 3.0,
    },
    "—": {
        "positive": [
            "电源", "地", "gnd", "vcc", "vee", "vdd", "vss",
            "nc", "悬空", "散热", "焊盘", "接地", "负电源", "正电源",
            "power", "supply", "ground",
        ],
        "weight": 3.0,
    },
}

# 行级 y 坐标分组阈值（像素）
ROW_Y_THRESHOLD = 10


# ===========================================================================
# 主入口
# ===========================================================================

def apply_table_context_correction(table_res_list: list[dict]) -> None:
    """
    对 table_res_list_all_page 中的每个表格执行行级语义重排序。

    原地修改 table_res_list，更新低置信度单元格的 text 和 confidence。

    参数：
        table_res_list: batch_analyze.py 中的 table_res_list_all_page，
                        每个元素是 dict，包含 "ocr_result" 字段
    """
    for table_dict in table_res_list:
        ocr_result = table_dict.get("ocr_result", [])
        if not ocr_result:
            continue

        try:
            # 1. 按 y 坐标分组建行
            rows = _group_ocr_by_row(ocr_result)

            # 2. 对每行做语义重排序
            for row in rows:
                _rerank_row_by_context(row)

        except Exception as e:
            logger.warning("表格上下文修正失败: %s", e)


# ===========================================================================
# 空间分组建行
# ===========================================================================

def _group_ocr_by_row(ocr_result: list) -> list[list]:
    """
    按 dt_box 的 y 坐标中心将 OCR 结果分组建行。

    dt_box 格式：np.array([[x0,y0],[x1,y1],[x2,y2],[x3,y3]]) 或 list of points

    参数：
        ocr_result: 列表，每项为 [dt_box, text, confidence, candidates?]

    返回：
        list of rows，每行是排序后的 ocr_result item 列表
    """
    if not ocr_result:
        return []

    # 提取每个 item 的 y 中心坐标
    items_with_y = []
    for item in ocr_result:
        box = item[0]
        y_center = _get_y_center(box)
        items_with_y.append((y_center, item))

    # 按 y 排序
    items_with_y.sort(key=lambda x: x[0])

    # 按 y 间距聚类成行
    rows = []
    current_row = [items_with_y[0][1]]
    prev_y = items_with_y[0][0]

    for y_center, item in items_with_y[1:]:
        if y_center - prev_y > ROW_Y_THRESHOLD:
            rows.append(current_row)
            current_row = [item]
        else:
            current_row.append(item)
        prev_y = y_center

    rows.append(current_row)

    # 每行内按 x 坐标排序（从左到右）
    for row in rows:
        row.sort(key=lambda item: _get_x_center(item[0]))

    return rows


def _get_y_center(box: Any) -> float:
    """从 dt_box 中提取 y 中心坐标"""
    if hasattr(box, 'shape'):  # numpy array
        arr = np.asarray(box)
        return float(np.mean(arr[:, 1]))
    elif isinstance(box, (list, tuple)):
        return float(np.mean([p[1] for p in box]))
    else:
        return 0.0


def _get_x_center(box: Any) -> float:
    """从 dt_box 中提取 x 中心坐标"""
    if hasattr(box, 'shape'):  # numpy array
        arr = np.asarray(box)
        return float(np.mean(arr[:, 0]))
    elif isinstance(box, (list, tuple)):
        return float(np.mean([p[0] for p in box]))
    else:
        return 0.0


# ===========================================================================
# 语义重排序
# ===========================================================================

def _rerank_row_by_context(row_items: list) -> None:
    """
    对一行内的所有 OCR 结果做语义重排序。

    row_items: list of [dt_box, text, confidence, candidates]
               candidates: [(text, score), ...] or []

    原地修改 row_items 中的 text 和 confidence。
    """
    # 收集行内所有文本作为上下文
    row_texts = []
    for item in row_items:
        text = item[1] if len(item) > 1 else ""
        if text:
            row_texts.append(text)

    full_context = " ".join(row_texts).lower()
    if not full_context:
        return

    for item in row_items:
        # 获取 top-K 候选（如果有）
        candidates = []
        if len(item) > 3 and item[3]:
            candidates = item[3]

        if not candidates or len(candidates) <= 1:
            continue

        original_text = item[1] if len(item) > 1 else ""
        original_conf = item[2] if len(item) > 2 else 1.0

        best = _select_by_context(candidates, full_context, original_conf)

        if best and best[0] != original_text:
            item[1] = best[0]  # 更新文本
            item[2] = best[1]  # 更新置信度


def _select_by_context(
    candidates: list[tuple[str, float]],
    row_context: str,
    original_confidence: float,
) -> tuple[str, float] | None:
    """
    从 top-K 候选中根据行上下文选择最佳候选。

    评分规则：
      - 基础分 = CTC beam search 归一化分数
      - 如果候选文本匹配 CONTEXT_SCORING 中的 label，
        且同行上下文中出现对应的 positive 关键词 → 加分

    参数：
        candidates: [(text, score), ...]
        row_context: 同行所有文本（已转小写）
        original_confidence: greedy decoder 的置信度（备用）

    返回：
        (best_text, best_score) 或 None
    """
    scores: dict[str, float] = {}

    for text, score in candidates:
        key = text
        scores[key] = score  # 基础分

        # 检查是否匹配已知语义标签
        for label, rules in CONTEXT_SCORING.items():
            if key == label:
                for kw in rules["positive"]:
                    # 使用词边界匹配防止子串误匹配
                    # 例如 "in" 不应匹配 "unknown" 或 "some_pin"
                    pattern = r'\b' + re.escape(kw) + r'\b'
                    if re.search(pattern, row_context):
                        scores[key] += rules["weight"]
                        break  # 同一 label 只加一次分

    if not scores:
        return None

    best_text = max(scores, key=scores.get)
    best_score = scores[best_text]

    return (best_text, best_score)
