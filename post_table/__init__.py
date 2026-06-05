"""
表格 OCR 识别结果后处理模块

该模块用于修正 MinerU pipeline 后端表格 OCR 中的字符混淆问题。

背景：
    MinerU pipeline 后端使用中文 PP-OCRv4 模型对表格单元格进行 OCR 识别。
    该模型的字符字典 (ppocrv4_doc_dict.txt) 包含 6625+ 个字符，其中：
    - 数字 0/1 的字典索引非常靠前（第 26/93 行），在训练数据中出现频繁
    - 英文字母 I/O 的索引非常靠后（第 3587/4741 行），出现频率低
    - 长破折号 — 索引更靠后（第 5550 行）
    
    对于视觉上难以区分的字符对（I/1、O/0、—/二），CTC 解码器天然倾向于
    输出字典索引更靠前、训练数据更多的字符，导致：
      I → 1,  O → 0,  — → 二
    
    本模块通过分析表格的列级上下文来安全地修正这些误识别。

使用方式：
    from post_table.fix_ocr_table import fix_markdown_file
    
    # 修正单个 markdown 文件
    fix_markdown_file("output.md", "output_fixed.md")
    
    # 或在代码中直接处理字符串
    corrected = fix_markdown_tables(original_md_content)
"""

from .fix_ocr_table import (
    fix_markdown_file,
    fix_markdown_tables,
    fix_html_table,
    correct_table_cell,
)
from .context_correct import apply_table_context_correction
from .expand_rowspan import expand_rowspan, expand_colspan

__all__ = [
    "fix_markdown_file",
    "fix_markdown_tables",
    "fix_html_table",
    "correct_table_cell",
    "apply_table_context_correction",
    "expand_rowspan",
    "expand_colspan",
]