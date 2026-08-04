"""MinerU 全部表格及页码导出 API。

上传一个 PDF，调用现有 ``parse_doc.py`` 完成解析和表格后处理，再返回最终
表格 HTML、表题和 1 基页码组成的 JSON 文件。

运行：
    python table_export_api.py

调用示例：
    curl -X POST http://localhost:5003/api/extract-pdf-tables \
      -F "file=@/root/autodl-tmp/pdfs/example.pdf" \
      -o example_tables.json
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, after_this_request, jsonify, request, send_file
from werkzeug.utils import secure_filename

from table_exporter import export_tables_from_parse_artifacts, find_parse_artifacts


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_BACKEND = os.environ.get("MINERU_BACKEND", "hybrid-auto-engine")
DEFAULT_METHOD = os.environ.get("MINERU_PARSE_METHOD", "auto")
DEFAULT_LANG = os.environ.get("MINERU_LANG", "ch")
MAX_PARSE_SECONDS = int(os.environ.get("TABLE_EXPORT_API_TIMEOUT", "3600"))


def allowed_pdf(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() == "pdf"


def str_from_request(name: str, default: str) -> str:
    value = request.form.get(name, request.args.get(name))
    return str(value).strip() if value is not None and str(value).strip() else default


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "message": "MinerU PDF table/page export API",
            "health": "/health",
            "endpoint": "/api/extract-pdf-tables",
            "method": "POST",
            "form_fields": {
                "file": "PDF file",
                "backend": f"optional, default {DEFAULT_BACKEND}",
                "method": f"optional, default {DEFAULT_METHOD}",
                "lang": f"optional, default {DEFAULT_LANG}",
            },
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/extract-pdf-tables", methods=["POST"])
def extract_pdf_tables():
    """接收 PDF，完成解析后返回全部最终表格及其页码。"""

    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    uploaded = request.files["file"]
    if not uploaded or uploaded.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_pdf(uploaded.filename):
        return jsonify({"error": "Invalid file type, please upload PDF"}), 400

    original_filename = Path(uploaded.filename).name
    filename = secure_filename(original_filename) or "uploaded.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    backend = str_from_request("backend", DEFAULT_BACKEND)
    method = str_from_request("method", DEFAULT_METHOD)
    lang = str_from_request("lang", DEFAULT_LANG)

    tmpdir = tempfile.mkdtemp(prefix="mineru_table_export_api_")
    try:
        tmp_root = Path(tmpdir)
        pdf_path = tmp_root / filename
        parse_output_dir = tmp_root / "mineru_output"
        response_path = tmp_root / f"{Path(filename).stem}_tables.json"

        uploaded.save(pdf_path)
        logging.info("Received PDF: %s", filename)
        run_parse_pipeline(pdf_path, parse_output_dir, backend, method, lang)

        final_json_path, middle_json_path = find_parse_artifacts(
            parse_output_dir,
            Path(filename).stem,
        )
        payload = export_tables_from_parse_artifacts(
            final_json_path,
            middle_json_path,
            source_file=original_filename,
        )
        response_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logging.info("Exported %d table(s): %s", payload["table_count"], response_path)

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return response

        return send_file(
            response_path,
            as_attachment=True,
            download_name=response_path.name,
            mimetype="application/json",
        )

    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("Table export timed out")
        return jsonify({"error": "Table export timed out", "detail": str(exc)}), 500
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("Table export failed")
        return jsonify(
            {
                "error": "Table export failed",
                "return_code": exc.returncode,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            }
        ), 500
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("PDF table export API failed")
        return jsonify({"error": "PDF table export API failed", "detail": str(exc)}), 500


def run_parse_pipeline(
    pdf_path: Path,
    parse_output_dir: Path,
    backend: str,
    method: str,
    lang: str,
) -> None:
    """使用与现有接口相同的 Python 环境调用 parse_doc.py。"""

    command = [
        sys.executable,
        str(PROJECT_ROOT / "parse_doc.py"),
        str(pdf_path),
        "-o",
        str(parse_output_dir),
        "-b",
        backend,
        "-m",
        method,
        "--lang",
        lang,
    ]
    logging.info("Running parse command: %s", " ".join(command))
    subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        timeout=MAX_PARSE_SECONDS,
    )


if __name__ == "__main__":
    port = int(os.environ.get("TABLE_EXPORT_API_PORT", "5003"))
    app.run(host="0.0.0.0", port=port, debug=False)
