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
import importlib.util  # 动态加载 modify 下的独立过滤脚本
import json       # 读写 middle_json
import os        # 环境变量设置
import sys       # sys.path / sys.exit
from pathlib import Path  # 路径操作

# MinerU 源码路径
MINERU_HOME = Path("/root/autodl-tmp/MinerU")
if MINERU_HOME.exists():
    sys.path.insert(0, str(MINERU_HOME))

from mineru.cli.common import do_parse, read_fn  # do_parse: 主解析函数, read_fn: 文件读取
from mineru.utils.enum_class import MakeMode  # Markdown 重建模式
from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make as vlm_union_make
from mineru.backend.office.office_middle_json_mkcontent import union_make as office_union_make

# =====================================================================
# [新增] 页眉页脚过滤模块
# 功能：MinerU 的版面分类有时只有 abandon/discarded，或把页眉页脚误判为正文。
#       这里在解析完成后读取 middle_json，把明确类型或重复出现在页边的
#       页眉、页脚、页码移动到 discarded_blocks，再重建最终 Markdown。
# 注意：该过滤只重建 .md；content_list 保持 MinerU 原始输出，便于排查和追溯。
# =====================================================================
HEADER_FOOTER_FILTER_PATH = Path("/root/autodl-tmp/modify/Filter_ headers_and_footers.py")
if not HEADER_FOOTER_FILTER_PATH.exists():
    HEADER_FOOTER_FILTER_PATH = Path(__file__).resolve().parent / "modify/Filter_ headers_and_footers.py"


def _load_header_footer_filter():
    if not HEADER_FOOTER_FILTER_PATH.exists():
        print(f"  ⏭️  跳过页眉页脚过滤（脚本不存在）: {HEADER_FOOTER_FILTER_PATH}")
        return None

    spec = importlib.util.spec_from_file_location(
        "header_footer_filter",
        HEADER_FOOTER_FILTER_PATH,
    )
    if spec is None or spec.loader is None:
        print(f"  ⏭️  跳过页眉页脚过滤（脚本无法加载）: {HEADER_FOOTER_FILTER_PATH}")
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HEADER_FOOTER_FILTER = _load_header_footer_filter()


def _guess_union_make(output_subdir: Path):
    """根据 MinerU 输出子目录判断应该用哪套 Markdown 生成逻辑。"""
    name = output_subdir.name
    if name in {"auto", "ocr", "txt"}:
        return pipeline_union_make
    if name.startswith("vlm") or name.startswith("hybrid"):
        return vlm_union_make
    if name == "office":
        return office_union_make
    return None


def apply_header_footer_filter(
    doc_output_dir: Path,
    source_pdf_path: Path | None = None,
) -> tuple[int, int]:
    """
    对单个文档输出目录执行页眉页脚过滤，并重建 Markdown。

    如果传入 source_pdf_path，会优先渲染 PDF 检测上下粗横线：
      - 上方粗横线以上视为页眉区
      - 下方粗横线以下视为页脚区
    检测不到横线时自动回退到重复文本/页码/首页声明规则。

    返回：
        (filtered_md_count, skip_count)
    """
    if HEADER_FOOTER_FILTER is None:
        return 0, 1

    if not doc_output_dir.is_dir():
        print(f"  ⏭️  跳过页眉页脚过滤（输出目录不存在）: {doc_output_dir}")
        return 0, 1

    filtered_count = 0
    skip_count = 0
    output_subdirs = [
        subdir
        for subdir in sorted(doc_output_dir.iterdir())
        if subdir.is_dir() and subdir.name != ".ipynb_checkpoints"
    ]

    for output_subdir in output_subdirs:
        make_func = _guess_union_make(output_subdir)
        if make_func is None:
            print(f"  ⏭️  跳过页眉页脚过滤（未知输出模式）: {output_subdir}")
            skip_count += 1
            continue

        middle_files = sorted(output_subdir.glob("*_middle.json"))
        if not middle_files:
            print(f"  ⏭️  跳过页眉页脚过滤（middle_json 不存在）: {output_subdir}")
            skip_count += 1
            continue

        for middle_path in middle_files:
            try:
                middle_json = json.loads(middle_path.read_text(encoding="utf-8"))
                pdf_info = HEADER_FOOTER_FILTER.load_pdf_info_list(middle_json)
                stats = HEADER_FOOTER_FILTER.filter_headers_and_footers(
                    pdf_info,
                    pdf_path=source_pdf_path,
                )

                md_path = output_subdir / f"{middle_path.name[:-len('_middle.json')]}.md"
                image_dir = "images"
                md_content = make_func(pdf_info, MakeMode.MM_MD, image_dir)

                middle_path.write_text(
                    json.dumps(middle_json, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
                md_path.write_text(md_content or "", encoding="utf-8")

                removed = stats.get("total_removed", 0)
                print(f"  ✅ 页眉页脚过滤: {md_path.name}  ({output_subdir.name}, 移除 {removed} 个 block)")
                filtered_count += 1
            except Exception as e:
                print(f"  ⚠️  页眉页脚过滤失败: {middle_path} -> {e}")
                skip_count += 1

    return filtered_count, skip_count

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


def apply_post_table_correction(doc_output_dir: Path) -> tuple[int, int]:
    """
    对单个文档输出目录中的 Markdown 文件执行表格 OCR 后处理。

    MinerU 不同 backend 的输出目录结构不同：
      - pipeline: doc_output_dir/auto/
      - vlm-*: doc_output_dir/vlm_*/
      - hybrid-*: doc_output_dir/hybrid_*/
      - office: doc_output_dir/office/

    返回：
        (fixed_count, skip_count)
    """
    if not doc_output_dir.is_dir():
        print(f"  ⏭️  跳过（输出目录不存在）: {doc_output_dir}")
        return 0, 1

    output_subdirs = [
        subdir
        for subdir in sorted(doc_output_dir.iterdir())
        if subdir.is_dir() and subdir.name != ".ipynb_checkpoints"
    ]

    if not output_subdirs:
        print(f"  ⏭️  跳过（没有输出子目录）: {doc_output_dir}")
        return 0, 1

    fixed_count = 0
    skip_count = 0
    for output_subdir in output_subdirs:
        md_files = sorted(output_subdir.rglob("*.md"))
        if not md_files:
            print(f"  ⏭️  跳过（markdown 不存在）: {output_subdir}")
            skip_count += 1
            continue

        for md_path in md_files:
            fix_markdown_file(str(md_path))  # output_path=None -> 覆盖原文件
            print(f"  ✅ 已修正: {md_path.name}  ({md_path.parent.name})")
            fixed_count += 1

    return fixed_count, skip_count


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
        "--image-analysis", action="store_true",
        help="启用 VLM 图片/图表内容分析（默认关闭，避免输出原文没有的 details 内容）"
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="兼容旧参数：禁用 VLM 图片/图表内容分析"
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
    source_files = []     # 实际读取成功的源文件，用于后处理阶段定位原始 PDF

    for f in files:
        try:
            pdf_bytes = read_fn(f)  # 读取文件（自动识别 PDF/图片/Office）
            pdf_file_names.append(f.stem)  # f.stem = 去掉后缀的文件名
            pdf_bytes_list.append(pdf_bytes)
            p_lang_list.append(args.lang)  # 每份文档都用相同语言
            source_files.append(f)
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
            image_analysis=args.image_analysis and not args.no_images,  # 是否分析图片/图表内容
        )
        print("-" * 50)
        print(f"解析完成！结果已保存至: {output_dir}")

        # =====================================================================
        # [新增] 页眉页脚过滤
        # 先基于 middle_json 移除页边重复文本/页码，再重建最终 Markdown。
        # 必须放在表格 OCR 后处理之前，否则重建 Markdown 会覆盖表格修正结果。
        # =====================================================================
        print("\n" + "=" * 50)
        print("页眉页脚过滤...")
        header_footer_count = 0
        header_footer_skip_count = 0
        for pdf_name, source_file in zip(pdf_file_names, source_files):
            source_pdf_path = source_file if source_file.suffix.lower() == ".pdf" else None
            doc_filtered_count, doc_skip_count = apply_header_footer_filter(
                Path(output_dir) / pdf_name,
                source_pdf_path=source_pdf_path,
            )
            header_footer_count += doc_filtered_count
            header_footer_skip_count += doc_skip_count
        print(f"页眉页脚过滤完成：处理 {header_footer_count} 个文件，跳过 {header_footer_skip_count} 个文件")

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
            doc_fixed_count, doc_skip_count = apply_post_table_correction(
                Path(output_dir) / pdf_name
            )
            fixed_count += doc_fixed_count
            skip_count += doc_skip_count
        print(f"后处理完成：修正 {fixed_count} 个文件，跳过 {skip_count} 个文件")
    except Exception as e:
        print(f"解析失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
