"""
MinerU pin/package extraction API.

Upload one PDF, run the existing parse + extract pipeline, and return the
extracted JSON file.

Run:
    python extract_api.py

Example:
    curl -X POST http://localhost:5002/api/extract-pdf-json \
      -F "file=@/root/autodl-tmp/pdfs/example.pdf" \
      -o example.json
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, after_this_request, jsonify, request, send_file
from werkzeug.utils import secure_filename


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
DEFAULT_SEMANTIC_CLASSIFY = os.environ.get("EXTRACT_SEMANTIC_CLASSIFY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MAX_EXTRACT_SECONDS = int(os.environ.get("EXTRACT_API_TIMEOUT", "3600"))


def allowed_pdf(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() == "pdf"


def bool_from_request(name: str, default: bool = False) -> bool:
    value = request.form.get(name, request.args.get(name))
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def str_from_request(name: str, default: str) -> str:
    value = request.form.get(name, request.args.get(name))
    return str(value).strip() if value is not None and str(value).strip() else default


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "message": "MinerU PDF pin/package extraction API",
            "health": "/health",
            "endpoint": "/api/extract-pdf-json",
            "method": "POST",
            "form_fields": {
                "file": "PDF file",
                "backend": f"optional, default {DEFAULT_BACKEND}",
                "method": f"optional, default {DEFAULT_METHOD}",
                "lang": f"optional, default {DEFAULT_LANG}",
                "semantic_classify": f"optional, default {DEFAULT_SEMANTIC_CLASSIFY}",
            },
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/extract-pdf-json", methods=["POST"])
def extract_pdf_json():
    """
    Receive PDF -> parse with MinerU -> extract pin/package fields -> return JSON.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    uploaded = request.files["file"]
    if not uploaded or uploaded.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_pdf(uploaded.filename):
        return jsonify({"error": "Invalid file type, please upload PDF"}), 400

    filename = secure_filename(uploaded.filename)
    backend = str_from_request("backend", DEFAULT_BACKEND)
    method = str_from_request("method", DEFAULT_METHOD)
    lang = str_from_request("lang", DEFAULT_LANG)
    semantic_classify = bool_from_request("semantic_classify", DEFAULT_SEMANTIC_CLASSIFY)

    tmpdir = tempfile.mkdtemp(prefix="mineru_extract_api_")
    try:
        tmp_root = Path(tmpdir)
        pdf_path = tmp_root / filename
        parse_output_dir = tmp_root / "mineru_output"
        extract_output = tmp_root / f"{Path(filename).stem}.json"
        summary_output = tmp_root / f"{Path(filename).stem}_info.json"

        uploaded.save(pdf_path)
        logging.info("Received PDF: %s", filename)

        run_extract_pipeline(
            pdf_path=pdf_path,
            parse_output_dir=parse_output_dir,
            extract_output=extract_output,
            summary_output=summary_output,
            backend=backend,
            method=method,
            lang=lang,
            semantic_classify=semantic_classify,
        )

        if not extract_output.exists():
            raise FileNotFoundError(f"Extraction JSON not found: {extract_output}")

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return response

        return send_file(
            extract_output,
            as_attachment=True,
            download_name=f"{Path(filename).stem}.json",
            mimetype="application/json",
        )

    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("Extraction timed out")
        return jsonify(
            {
                "error": "Extraction timed out",
                "detail": str(exc),
            }
        ), 500
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("Extraction failed")
        return jsonify(
            {
                "error": "Extraction failed",
                "return_code": exc.returncode,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            }
        ), 500
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("PDF extraction API failed")
        return jsonify(
            {
                "error": "PDF extraction API failed",
                "detail": str(exc),
            }
        ), 500


def run_extract_pipeline(
    pdf_path: Path,
    parse_output_dir: Path,
    extract_output: Path,
    summary_output: Path,
    backend: str,
    method: str,
    lang: str,
    semantic_classify: bool,
) -> None:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "extract.py"),
        str(pdf_path),
        "-o",
        str(parse_output_dir),
        "-b",
        backend,
        "-m",
        method,
        "--lang",
        lang,
        "--extract-output",
        str(extract_output),
        "--summary-output",
        str(summary_output),
    ]
    if semantic_classify:
        command.append("--semantic-classify")

    logging.info("Running extraction command: %s", " ".join(command))
    subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        timeout=MAX_EXTRACT_SECONDS,
    )


if __name__ == "__main__":
    port = int(os.environ.get("EXTRACT_API_PORT", "5002"))
    app.run(host="0.0.0.0", port=port, debug=False)
