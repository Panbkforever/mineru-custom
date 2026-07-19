"""
MinerU PDF 解析 API（返回 Zip 压缩包）

接收 PDF 上传 → MinerU 解析（Python API 调用）→ 将全部产出打包为 zip → 返回

用法:
    python mineru_api_zip.py
"""

'''
执行命令:
curl -X POST http://localhost:5001/api/process-pdf-zip \
  -F "file=@/root/autodl-tmp/pdfs/5b1d01ebfc56798c0741e577911781f2.pdf" \
  -o output.zip
'''

import os
import re
import sys
import time
import json
import shutil
import logging
import tempfile
import zipfile
import importlib.util
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename

# =========================
# MinerU 环境设置（与 parse_doc.py 一致）
# =========================
MINERU_HOME = Path("/root/autodl-tmp/MinerU")
if MINERU_HOME.exists():
    sys.path.insert(0, str(MINERU_HOME))

os.environ.setdefault("MINERU_MODEL_SOURCE", "modelscope")
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from mineru.cli.common import do_parse, read_fn  # noqa: E402
from mineru.utils.enum_class import MakeMode  # noqa: E402
from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make  # noqa: E402
from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make as vlm_union_make  # noqa: E402
from mineru.backend.office.office_middle_json_mkcontent import union_make as office_union_make  # noqa: E402

# =========================
# 页眉页脚过滤模块
# =========================
# 说明：
# MinerU 的区域分类不一定总能给出明确 header/footer，有些页眉页脚会被当成正文。
# 这里在 do_parse 完成后读取 middle_json，将明确类型或页边重复文本/页码移入
# discarded_blocks，再重建最终 Markdown。该步骤只影响 .md，便于最终结果去掉页眉页脚。
HEADER_FOOTER_FILTER_PATH = Path("/root/autodl-tmp/modify/Filter_ headers_and_footers.py")
if not HEADER_FOOTER_FILTER_PATH.exists():
    HEADER_FOOTER_FILTER_PATH = Path(__file__).resolve().parent / "modify/Filter_ headers_and_footers.py"


def load_header_footer_filter():
    if not HEADER_FOOTER_FILTER_PATH.exists():
        logging.warning("Header/footer filter script not found: %s", HEADER_FOOTER_FILTER_PATH)
        return None

    spec = importlib.util.spec_from_file_location(
        "header_footer_filter",
        HEADER_FOOTER_FILTER_PATH,
    )
    if spec is None or spec.loader is None:
        logging.warning("Header/footer filter script cannot be loaded: %s", HEADER_FOOTER_FILTER_PATH)
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HEADER_FOOTER_FILTER = load_header_footer_filter()

# =========================
# post_table 后处理模块导入
# =========================
sys.path.insert(0, str(Path("/root/autodl-tmp")))
try:
    from post_table.fix_ocr_table import fix_markdown_file as fix_table_ocr
    from post_table.min_max_coordinate_correct import (
        correct_min_max_tables_in_middle_json,
    )
    from post_table.restore_cell_line_breaks import (
        restore_cell_line_breaks_in_middle_json,
    )
    from post_table.split_merged_tables import split_merged_tables_in_file
    POST_TABLE_AVAILABLE = True
    logging.info("post_table module loaded successfully")
except ImportError as e:
    POST_TABLE_AVAILABLE = False
    correct_min_max_tables_in_middle_json = None
    restore_cell_line_breaks_in_middle_json = None
    split_merged_tables_in_file = None
    logging.warning("post_table module not available: %s", e)

# =========================
# 基础配置
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB
# ALLOWED_EXTENSIONS = {"pdf"}
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tiff", "bmp"}

# 输出根目录
OUTPUT_DIR = Path("/root/autodl-tmp/mineru_output")

# MinerU 解析后端配置
# 可选值:
#   - "pipeline": 传统 OCR + 布局检测流水线（速度快，适合常规文档）
#   - "vlm-auto-engine": VLM 自动选择引擎（精度高，适合复杂文档）
#   - "vlm-transformers": VLM Transformers 后端
#   - "vlm-vllm-engine": VLM vLLM 后端（需要额外安装 vllm）
#   - "hybrid-auto-engine": 混合模式（VLM + Pipeline），公式识别用 VLM，其他用 Pipeline
#   - "hybrid-vllm-engine": 混合模式 + vLLM 后端
MINERU_BACKEND = os.environ.get("MINERU_BACKEND", "hybrid-auto-engine")
IMAGE_ANALYSIS = os.environ.get("MINERU_IMAGE_ANALYSIS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# 等待 MinerU 产出的最大秒数
MAX_WAIT_SECONDS = 1800   # 30 分钟
POLL_INTERVAL = 2         # 每 2 秒轮询一次


# =========================
# 工具函数
# =========================
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def sanitize_name(name: str) -> str:
    """将非法字符替换成下划线"""
    return re.sub(r'[\/\\:\*\?"<>\| ]', "_", name)


def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def clear_previous_output(pdf_stem: str):
    """如果同名 PDF 之前已经跑过，先删旧输出"""
    safe_stem = sanitize_name(pdf_stem)
    outdir = OUTPUT_DIR / safe_stem
    if outdir.exists():
        logging.info("Removing previous output dir: %s", outdir)
        shutil.rmtree(outdir, ignore_errors=True)


def wait_for_output(outdir: Path, timeout: int = MAX_WAIT_SECONDS) -> Path:
    """
    轮询等待输出目录生成。
    
    MinerU 不同 backend 的输出目录结构：
      - pipeline: outdir/auto/
      - vlm-*: outdir/vlm_*/
      - hybrid-*: outdir/hybrid_*/
    
    只要检测到任意子目录（auto/ 或 vlm_*/ 或 hybrid_*/）且有内容即视为完成。
    """
    start = time.time()
    while True:
        # 查找所有可能的输出子目录
        candidate_dirs = []
        if outdir.is_dir():
            for subdir in outdir.iterdir():
                if subdir.is_dir():
                    candidate_dirs.append(subdir)
        
        # 检查是否有候选目录且包含文件
        for candidate in candidate_dirs:
            has_output = any(candidate.iterdir())
            if has_output:
                logging.info("Output ready: %s", candidate)
                return outdir
        
        elapsed = time.time() - start
        if elapsed > timeout:
            # 超时前打印当前目录结构用于调试
            try:
                children = list(outdir.iterdir()) if outdir.is_dir() else []
                logging.warning(
                    "Timeout waiting for output. Current directory structure: %s",
                    [str(c.name) for c in children]
                )
            except Exception:
                pass
            raise TimeoutError(f"Timed out waiting for MinerU output in: {outdir}")
        
        time.sleep(POLL_INTERVAL)


def guess_union_make(output_subdir: Path):
    """根据 MinerU 输出子目录判断使用哪套 Markdown 生成逻辑。"""
    name = output_subdir.name
    if name in {"auto", "ocr", "txt"}:
        return pipeline_union_make
    if name.startswith("vlm") or name.startswith("hybrid"):
        return vlm_union_make
    if name == "office":
        return office_union_make
    return None


def apply_header_footer_filter(output_dir: Path, source_pdf_path: Path | None = None):
    """
    基于 middle_json 过滤页眉页脚，并重建最终 Markdown。

    注意：
      1. 必须放在 post_table 表格 OCR 后处理之前，否则重建 Markdown 会覆盖表格修正。
      2. content_list 暂时保留 MinerU 原始输出，方便排查结构化识别结果。
      3. 如果传入 source_pdf_path，会优先检测 PDF 页面上下粗横线作为正文边界。
    """
    if HEADER_FOOTER_FILTER is None:
        logging.warning("Header/footer filter unavailable, skipping")
        return

    if not output_dir.is_dir():
        logging.warning("Output dir not found, skipping header/footer filter: %s", output_dir)
        return

    processed = 0
    removed_total = 0
    for output_subdir in sorted(output_dir.iterdir()):
        if not output_subdir.is_dir() or output_subdir.name == ".ipynb_checkpoints":
            continue

        make_func = guess_union_make(output_subdir)
        if make_func is None:
            logging.info("Unknown output mode, skipping header/footer filter: %s", output_subdir)
            continue

        for middle_path in sorted(output_subdir.glob("*_middle.json")):
            try:
                middle_json = json.loads(middle_path.read_text(encoding="utf-8"))
                pdf_info = HEADER_FOOTER_FILTER.load_pdf_info_list(middle_json)
                # 与 parse_doc.py 保持一致：关闭横线边界推断，避免把表格边框
                # 当成页脚分界线后误删位于页面下方的完整表格。
                filter_config = HEADER_FOOTER_FILTER.FilterConfig(
                    enable_line_boundary_filter=False,
                )
                stats = HEADER_FOOTER_FILTER.filter_headers_and_footers(
                    pdf_info,
                    pdf_path=source_pdf_path,
                    config=filter_config,
                )
                # 修正 MIN/TYP/NOM/MAX 中的重复与错位，并依据原始
                # colspan 完整展开真正横跨多个极限值列的单元格。
                # PDF 坐标只负责定位，最终仍保留 VLM 原 HTML 内容。
                min_max_stats = (
                    correct_min_max_tables_in_middle_json(
                        middle_json,
                        source_pdf_path,
                    )
                    if correct_min_max_tables_in_middle_json is not None
                    else {}
                )
                line_break_stats = (
                    restore_cell_line_breaks_in_middle_json(
                        middle_json,
                        source_pdf_path,
                    )
                    if restore_cell_line_breaks_in_middle_json is not None
                    else {}
                )

                md_path = output_subdir / f"{middle_path.name[:-len('_middle.json')]}.md"
                md_content = make_func(pdf_info, MakeMode.MM_MD, "images")

                middle_path.write_text(
                    json.dumps(middle_json, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
                md_path.write_text(md_content or "", encoding="utf-8")

                processed += 1
                removed_total += stats.get("total_removed", 0)
                logging.info(
                    "Header/footer and limit-column alignment processed: %s "
                    "(%s, removed %d block(s), split %d merged column(s), "
                    "corrected %d row(s), protected %d nowrap cell(s), "
                    "restored %d cell break(s))",
                    md_path.name,
                    output_subdir.name,
                    stats.get("total_removed", 0),
                    min_max_stats.get("columns_added", 0),
                    min_max_stats.get("rows_corrected", 0),
                    min_max_stats.get("nowrap_cells", 0),
                    line_break_stats.get("breaks_added", 0),
                )
            except Exception as e:
                logging.warning("Header/footer filter failed for %s: %s", middle_path, e)

    logging.info(
        "Header/footer filter complete: processed=%d, removed_blocks=%d",
        processed,
        removed_total,
    )


def run_mineru_pdf(pdf_path: Path) -> Path:
    """
    使用 MinerU Python API 解析 PDF：
    1. 清理之前的同名输出
    2. 调用 do_parse
    3. 等待输出目录就绪
    4. 返回输出目录路径
    """
    ensure_dirs()

    pdf_name = pdf_path.stem
    safe_stem = sanitize_name(pdf_name)

    # 清理旧输出，防止残留
    clear_previous_output(pdf_name)

    # 读取 PDF 字节
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    output_dir = str(OUTPUT_DIR.resolve())

    logging.info("Parsing PDF: %s", pdf_path.name)
    logging.info("Output dir: %s", output_dir)
    logging.info("Backend: %s", MINERU_BACKEND)

    # 调用 MinerU 解析引擎
    do_parse(
        output_dir=output_dir,
        pdf_file_names=[pdf_name],
        pdf_bytes_list=[pdf_bytes],
        p_lang_list=["ch"],            # 默认中文，可根据需要改为参数
        backend=MINERU_BACKEND,        # 使用配置的后端（pipeline 或 vlm-*）
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
        f_draw_layout_bbox=True,
        f_draw_span_bbox=True,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_pdf=False,
        f_dump_content_list=True,
        start_page_id=0,
        end_page_id=None,
        image_analysis=IMAGE_ANALYSIS,
    )

    target_outdir = OUTPUT_DIR / safe_stem
    wait_for_output(target_outdir)

    # 先重建去掉页眉页脚的 Markdown，再做表格 OCR 修正。
    apply_header_footer_filter(target_outdir, source_pdf_path=pdf_path)

    # 应用 post_table 后处理修正
    apply_post_table_correction(target_outdir)

    return target_outdir


def apply_post_table_correction(output_dir: Path):
    """
    对 MinerU 输出的 Markdown 文件执行表格结构和 OCR 后处理修正。
    
    处理内容：
      1. 拆分错误合并在一个 <table> 中的多个逻辑子表
      2. 修正表格中的字符混淆（I/1, O/0, 二/—）
      3. 展开 HTML 表格的 rowspan/colspan 合并单元格
    
    参数：
        output_dir: MinerU 输出目录（包含 auto/ 或 vlm_*/ 或 hybrid_*/ 子目录）
    """
    if not POST_TABLE_AVAILABLE:
        logging.warning("post_table module not available, skipping post-processing")
        return
    
    # 查找实际的输出子目录（auto/ 或 vlm_*/ 或 hybrid_*/）
    output_subdir = None
    if output_dir.is_dir():
        for subdir in output_dir.iterdir():
            if subdir.is_dir() and subdir.name != ".ipynb_checkpoints":
                output_subdir = subdir
                break
    
    if output_subdir is None:
        logging.warning("No output subdirectory found, skipping post-processing")
        return
    
    # 查找所有 .md 文件
    md_files = list(output_subdir.rglob("*.md"))
    if not md_files:
        logging.info("No markdown files found, skipping post-processing")
        return
    
    logging.info("Applying post-table correction to %d markdown file(s)", len(md_files))
    
    for md_file in md_files:
        try:
            logging.info("Processing: %s", md_file.name)
            # 在 colspan/rowspan 展开前，先按“全宽小节行 + 完整重复表头”
            # 拆开被 MinerU 错误放进同一 <table> 的多个逻辑子表。
            split_tables = split_merged_tables_in_file(md_file)
            # fix_markdown_file 会原地修改文件（output_path=None 时覆盖原文件）
            fix_table_ocr(str(md_file), output_path=None)
            write_final_markdown_json(md_file)
            logging.info(
                "Corrected: %s (split logical subtables: %d)",
                md_file.name,
                split_tables,
            )
        except Exception as e:
            logging.warning("Failed to correct %s: %s", md_file.name, e)


def write_final_markdown_json(md_path: Path) -> Path:
    """
    将最终 Markdown 同步保存为同名 JSON。

    这个 JSON 是最终 .md 的结构化副本，不是 MinerU 的 middle_json/model_json。
    它在 Markdown 表格后处理完成后生成，确保内容和最终 .md 一致。
    """
    md_content = md_path.read_text(encoding="utf-8")
    json_path = md_path.with_suffix(".json")
    payload = {
        "document_name": md_path.stem,
        "markdown_file": md_path.name,
        "markdown": md_content,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return json_path


def make_zip(output_dir: Path, zip_path: Path):
    """
    将 output_dir 下的全部文件打包为 zip
    （包含 md / json / images / pdf 等所有产出）
    """
    zip_path = Path(zip_path)

    logging.info("Creating zip: %s <- %s", zip_path, output_dir)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in output_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(output_dir.parent)
                zf.write(file_path, arcname)

    logging.info("Zip created: %s (size: %d bytes)", zip_path, zip_path.stat().st_size)


# =========================
# 路由
# =========================
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "message": "MinerU PDF to Markdown API (Zip)",
        "health": "/health",
        "endpoints": {
            "pdf": "/api/process-pdf-zip",
            "image": "/api/process-image-zip",
        },
        "method": "POST",
        "config": {
            "backend": MINERU_BACKEND,
            "image_analysis": IMAGE_ANALYSIS,
            "header_footer_filter": HEADER_FOOTER_FILTER is not None,
            "available_backends": [
                "pipeline (传统 OCR + 布局检测，速度快)",
                "vlm-auto-engine (VLM 自动选择，精度高)",
                "vlm-transformers (VLM Transformers)",
                "vlm-vllm-engine (VLM vLLM，需额外安装)",
                "hybrid-auto-engine (混合模式：公式用 VLM，其他用 Pipeline)",
                "hybrid-vllm-engine (混合模式 + vLLM)"
            ],
            "post_table_correction": POST_TABLE_AVAILABLE,
        },
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/process-pdf-zip", methods=["POST"])
def process_pdf_zip():
    """
    接收 PDF → MinerU 解析 → 返回全部产出的 zip 压缩包
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(f.filename):
        return jsonify({"error": "Invalid file type, please upload PDF"}), 400

    filename = secure_filename(f.filename)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 保存上传的 PDF 到临时目录
            tmp_pdf_path = Path(tmpdir) / filename
            f.save(tmp_pdf_path)

            logging.info("Received file: %s", filename)

            # 执行 MinerU 解析
            output_dir = run_mineru_pdf(tmp_pdf_path)

            # 打包为 zip
            zip_filename = f"{Path(filename).stem}_mineru_output.zip"
            zip_path = Path(tmpdir) / zip_filename
            make_zip(output_dir, zip_path)

            # 返回 zip
            return send_file(
                zip_path,
                as_attachment=True,
                download_name=zip_filename,
                mimetype="application/zip",
            )

    except TimeoutError as e:
        logging.exception("MinerU parsing timed out")
        return jsonify({
            "error": "MinerU parsing timed out",
            "detail": str(e),
        }), 500

    except Exception as e:
        logging.exception("PDF processing failed")
        return jsonify({
            "error": "PDF processing failed",
            "detail": str(e),
        }), 500


def run_mineru_image(image_path: Path) -> Path:
    """
    使用 MinerU Python API 解析图片（与 run_mineru_pdf 的区别：
    图片字节需要通过 read_fn 转成 PDF 字节，MinerU 内部统一用 PDF 流程处理）
    """
    ensure_dirs()

    img_name = image_path.stem
    safe_stem = sanitize_name(img_name)

    clear_previous_output(img_name)

    # 用 read_fn 读取图片，自动转换成 PDF 字节
    pdf_bytes = read_fn(image_path)

    output_dir = str(OUTPUT_DIR.resolve())

    logging.info("Parsing image: %s", image_path.name)
    logging.info("Backend: %s", MINERU_BACKEND)

    do_parse(
        output_dir=output_dir,
        pdf_file_names=[img_name],
        pdf_bytes_list=[pdf_bytes],
        p_lang_list=["ch"],
        backend=MINERU_BACKEND,        # 使用配置的后端（pipeline 或 vlm-*）
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
        f_draw_layout_bbox=True,
        f_draw_span_bbox=True,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_pdf=False,
        f_dump_content_list=True,
        start_page_id=0,
        end_page_id=None,
        image_analysis=IMAGE_ANALYSIS,
    )

    target_outdir = OUTPUT_DIR / safe_stem
    wait_for_output(target_outdir)

    # 先重建去掉页眉页脚的 Markdown，再做表格 OCR 修正。
    apply_header_footer_filter(target_outdir)

    # 应用 post_table 后处理修正
    apply_post_table_correction(target_outdir)

    return target_outdir


@app.route("/api/process-image-zip", methods=["POST"])
def process_image_zip():
    """
    接收图片（PNG/JPG 等）→ read_fn 转 PDF 字节 → MinerU 解析 → 返回全部产出的 zip
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "No file selected"}), 400

    ext = f.filename.rsplit(".", 1)[1].lower() if "." in f.filename else ""
    if ext not in {"png", "jpg", "jpeg", "tiff", "bmp"}:
        return jsonify({"error": "Invalid file type, please upload image (PNG/JPG/TIFF/BMP)"}), 400

    filename = secure_filename(f.filename)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_img_path = Path(tmpdir) / filename
            f.save(tmp_img_path)

            logging.info("Received image: %s", filename)

            output_dir = run_mineru_image(tmp_img_path)

            zip_filename = f"{Path(filename).stem}_mineru_output.zip"
            zip_path = Path(tmpdir) / zip_filename
            make_zip(output_dir, zip_path)

            return send_file(
                zip_path,
                as_attachment=True,
                download_name=zip_filename,
                mimetype="application/zip",
            )

    except TimeoutError as e:
        logging.exception("MinerU parsing timed out")
        return jsonify({
            "error": "MinerU parsing timed out",
            "detail": str(e),
        }), 500

    except Exception as e:
        logging.exception("Image processing failed")
        return jsonify({
            "error": "Image processing failed",
            "detail": str(e),
        }), 500


# =========================
# 启动
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
