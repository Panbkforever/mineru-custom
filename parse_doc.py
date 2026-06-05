"""
MinerU 文档解析脚本
使用 Python API 方式调用，替代命令行

用法:
    python parse_doc.py <文件路径> -o <输出目录>

示例:
    python parse_doc.py pdfs/example.pdf -o outputs
    python parse_doc.py pdfs/example.pdf -o outputs -b pipeline
    python parse_doc.py pdfs/example.pdf -o outputs --lang en
"""

import argparse  # 命令行参数解析
import os        # 环境变量设置
import sys       # sys.path / sys.exit
from pathlib import Path  # 路径操作

# MinerU 源码路径
MINERU_HOME = Path("/root/autodl-tmp/MinerU")
if MINERU_HOME.exists():
    sys.path.insert(0, str(MINERU_HOME))

from mineru.cli.common import do_parse, read_fn  # do_parse: 主解析函数, read_fn: 文件读取

# =====================================================================
# [新增] 表格 OCR 后处理模块
# 功能：修正 MinerU pipeline 后端中文 OCR 模型在表格中常见的
#       字符混淆问题：I→1、O→0、—→二，利用列级上下文做安全修正。
# 原理见 post_table/fix_ocr_table.py 头部注释。
# =====================================================================
POST_TABLE_DIR = Path("/root/autodl-tmp/post_table")
if POST_TABLE_DIR.exists():
    sys.path.insert(0, str(POST_TABLE_DIR.parent))
from post_table.fix_ocr_table import fix_markdown_file  # noqa: E402


def main():
    # 模型下载源设为 modelscope（国内镜像）
    os.environ.setdefault("MINERU_MODEL_SOURCE", "modelscope")

    # ---- 命令行参数定义 ----
    parser = argparse.ArgumentParser(description="MinerU 文档解析工具（Python API）")

    parser.add_argument("input_path", help="输入文件或目录路径")  # 必填，要解析的文件
    parser.add_argument("-o", "--output", default="./outputs", help="输出目录（默认: ./outputs）")
    parser.add_argument(
        "-b", "--backend",
        default="pipeline",  # 默认 pipeline 模式，CPU/GPU 均可
        choices=["pipeline", "vlm-auto-engine", "hybrid-auto-engine",
                 "vlm-http-client", "hybrid-http-client"],
        help="解析后端（默认: pipeline，通用性好）"
    )
    parser.add_argument(
        "-m", "--method",
        default="auto",  # 自动选择最优解析方式
        choices=["auto", "txt", "ocr"],
        help="解析方法（默认: auto）"
    )
    parser.add_argument(
        "--lang",
        default="ch",  # 中文
        help="文档语言（默认: ch），可选: en, japanese, korean, chinese_cht 等"
    )
    parser.add_argument(
        "--start-page", type=int, default=0,  # 从第1页开始
        help="起始页码（0开始，默认: 0）"
    )
    parser.add_argument(
        "--end-page", type=int, default=None,  # None=解析到最后一页
        help="结束页码（不含，默认: 到最后一页）"
    )
    parser.add_argument(
        "--no-formula", action="store_true",  # 默认启用公式解析
        help="禁用公式解析"
    )
    parser.add_argument(
        "--no-table", action="store_true",  # 默认启用表格解析
        help="禁用表格解析"
    )
    parser.add_argument(
        "--no-images", action="store_true",  # 默认提取图片
        help="不输出图片"
    )

    args = parser.parse_args()  # 解析参数

    input_path = Path(args.input_path)  # 用户输入的路径

    if not input_path.exists():  # 路径不存在则退出
        print(f"错误: 文件或目录不存在: {input_path}")
        sys.exit(1)

    output_dir = str(Path(args.output).resolve())  # 输出目录的绝对路径

    # ---- 收集待解析的文件 ----
    if input_path.is_dir():  # 输入是目录
        supported_suffixes = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
        files = sorted([  # 找出目录下所有支持的文档
            f for f in input_path.iterdir()
            if f.is_file() and f.suffix.lower() in supported_suffixes
        ])
        if not files:
            print(f"错误: 目录中没有支持的文档文件")
            sys.exit(1)
    else:
        files = [input_path]  # 单个文件

    # 打印基本信息
    print(f"解析后端: {args.backend}")
    print(f"输出目录: {output_dir}")
    print(f"待解析文件 ({len(files)} 个):")
    for f in files:
        print(f"  - {f.name}")
    print("-" * 50)

    # ---- 读取文件内容 ----
    pdf_file_names = []   # 文件名列表（不含后缀），用于输出目录命名
    pdf_bytes_list = []   # 文件二进制内容列表
    p_lang_list = []      # 文档语言列表

    for f in files:
        try:
            pdf_bytes = read_fn(f)  # 读取文件（自动识别 PDF/图片/Office）
            pdf_file_names.append(f.stem)  # f.stem = 去掉后缀的文件名
            pdf_bytes_list.append(pdf_bytes)
            p_lang_list.append(args.lang)  # 每份文档都用相同语言
        except Exception as e:
            print(f"  读取失败 {f.name}: {e}")
            continue

    if not pdf_bytes_list:
        print("没有可解析的文件")
        sys.exit(1)

    # ---- 执行解析 ----
    try:
        do_parse(
            output_dir=output_dir,              # 输出根目录
            pdf_file_names=pdf_file_names,      # 文件名列表
            pdf_bytes_list=pdf_bytes_list,      # 文件二进制数据
            p_lang_list=p_lang_list,            # 语言列表
            # 根据 pdfs 中的文件名自动设置语言
            backend=args.backend,               # 解析后端
            parse_method=args.method,           # 解析方法
            formula_enable=not args.no_formula, # 是否解析公式（输出 LaTeX）
            table_enable=not args.no_table,     # 是否解析表格（输出 HTML）
            f_draw_layout_bbox=True,            # 是否输出版面分析可视化 PDF
            f_draw_span_bbox=True,              # 是否输出 span 框可视化 PDF
            f_dump_md=True,                     # 是否输出 Markdown
            f_dump_middle_json=True,            # 是否输出中间格式 JSON
            f_dump_model_output=True,           # 是否输出模型原始输出 JSON
            f_dump_orig_pdf=False,              # 是否拷贝原始 PDF（False 保持简洁）
            f_dump_content_list=True,           # 是否输出内容列表 JSON
            start_page_id=args.start_page,      # 起始页码
            end_page_id=args.end_page,          # 结束页码
            image_analysis=not args.no_images,  # 是否提取并分析图片
        )
        print("-" * 50)
        print(f"解析完成！结果已保存至: {output_dir}")

        # =====================================================================
        # [新增] 解析完成后自动执行表格 OCR 后处理
        # 遍历每个输出目录中的 markdown 文件，对其中的表格执行字符修正：
        #   I→1、O→0、—→二（利用列级上下文做安全修正，避免误伤）
        # 直接覆盖 MinerU 的原始输出文件（fix_markdown_file 不传 output_path 即覆盖）
        # =====================================================================
        print("\n" + "=" * 50)
        print("表格 OCR 后处理...")
        fixed_count = 0
        skip_count = 0
        for pdf_name in pdf_file_names:
            md_path = Path(output_dir) / pdf_name / "auto" / f"{pdf_name}.md"
            if md_path.exists():
                fix_markdown_file(str(md_path))  # output_path=None → 覆盖原文件
                print(f"  ✅ 已修正: {md_path.name}  ({md_path.parent.parent.name})")
                fixed_count += 1
            else:
                print(f"  ⏭️  跳过（markdown 不存在）: {md_path}")
                skip_count += 1
        print(f"后处理完成：修正 {fixed_count} 个文件，跳过 {skip_count} 个文件")
    except Exception as e:
        print(f"解析失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
