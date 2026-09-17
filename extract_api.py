"""
MinerU pin/package extraction API.

Upload one PDF per request, run the existing parse + extract pipeline,
and return the final extraction JSON directly.

Run:
    cd /root/autodl-tmp
    source .env
    source MinerU/.venv/bin/activate
    python extract_api.py

Example:
    curl -X POST http://localhost:5002/api/extract-pdf-json-batch \
      -F "files=@/root/autodl-tmp/pdfs/a.pdf" \
      -o result.json
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from flask import Flask, Response, after_this_request, jsonify, request
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


def int_from_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        logging.warning("Invalid integer env %s=%r, use %s", name, os.environ.get(name), default)
        return default


def float_from_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        logging.warning("Invalid float env %s=%r, use %s", name, os.environ.get(name), default)
        return default


MAX_EXTRACT_SECONDS = int_from_env("EXTRACT_API_TIMEOUT", 3600)
DEFAULT_API_WORKERS = max(1, int_from_env("EXTRACT_API_WORKERS", 1))
DEFAULT_LLM_WORKERS = max(
    1,
    int_from_env(
        "EXTRACT_API_LLM_WORKERS",
        int_from_env("EXTRACT_LLM_WORKERS", 1),
    ),
)
DEFAULT_API_LOCK_DIR = Path(
    os.environ.get("EXTRACT_API_LOCK_DIR")
    or (Path(tempfile.gettempdir()) / "mineru_extract_api_pipeline_locks")
)
DEFAULT_LLM_LOCK_DIR = Path(
    os.environ.get("EXTRACT_API_LLM_LOCK_DIR")
    or os.environ.get("EXTRACT_LLM_LOCK_DIR")
    or (Path(tempfile.gettempdir()) / "mineru_extract_api_llm_locks")
)
DEFAULT_LLM_ASSIGN_DIR = Path(
    os.environ.get("EXTRACT_API_LLM_ASSIGN_DIR")
    or os.environ.get("EXTRACT_LLM_ASSIGN_DIR")
    or (Path(tempfile.gettempdir()) / "mineru_extract_api_llm_assign")
)
DEFAULT_LOCK_POLL_SECONDS = max(
    0.05,
    float_from_env("EXTRACT_API_LOCK_POLL_SECONDS", 0.2),
)


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
            "endpoint": "/api/extract-pdf-json-batch",
            "method": "POST",
            "form_fields": {
                "files": "exactly one PDF file",
                "backend": f"optional, default {DEFAULT_BACKEND}",
                "method": f"optional, default {DEFAULT_METHOD}",
                "lang": f"optional, default {DEFAULT_LANG}",
                "semantic_classify": f"optional, default {DEFAULT_SEMANTIC_CLASSIFY}",
            },
            "server_concurrency": {
                "api_workers": DEFAULT_API_WORKERS,
                "llm_workers": DEFAULT_LLM_WORKERS,
                "api_lock_dir": str(DEFAULT_API_LOCK_DIR),
                "llm_lock_dir": str(DEFAULT_LLM_LOCK_DIR),
            },
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "api_workers": DEFAULT_API_WORKERS,
            "llm_workers": DEFAULT_LLM_WORKERS,
        }
    ), 200


@app.route("/api/extract-pdf-json-batch", methods=["POST"])
def extract_pdf_json():
    """
    Receive PDF -> parse with MinerU -> extract pin/package fields -> return JSON.
    """
    uploaded_files = request.files.getlist("files") + request.files.getlist("file")
    uploaded_files = [item for item in uploaded_files if item and item.filename]
    if not uploaded_files:
        return jsonify({"error": "No PDF file selected; use form field 'files'"}), 400
    if len(uploaded_files) != 1:
        return jsonify(
            {
                "error": "Only one PDF is allowed per request. Send multiple HTTP requests for concurrency.",
                "received": len(uploaded_files),
            }
        ), 400

    uploaded = uploaded_files[0]
    original_filename = uploaded.filename or "uploaded.pdf"
    filename = secure_filename(original_filename) or "uploaded.pdf"

    if not allowed_pdf(filename):
        return jsonify({"error": "Invalid file type, please upload PDF"}), 400

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
        llm_key_index = assign_next_llm_key_index(DEFAULT_LLM_WORKERS)

        uploaded.save(pdf_path)
        logging.info(
            "Received PDF: %s, api_workers=%s, llm_workers=%s, llm_key_index=%s",
            original_filename,
            DEFAULT_API_WORKERS,
            DEFAULT_LLM_WORKERS,
            llm_key_index,
        )

        with api_request_slot():
            run_extract_pipeline(
                pdf_path=pdf_path,
                parse_output_dir=parse_output_dir,
                extract_output=extract_output,
                summary_output=summary_output,
                backend=backend,
                method=method,
                lang=lang,
                semantic_classify=semantic_classify,
                llm_key_index=llm_key_index,
            )

        if not extract_output.exists():
            raise FileNotFoundError(f"Extraction JSON not found: {extract_output}")

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return response

        return Response(
            extract_output.read_text(encoding="utf-8"),
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
    llm_key_index: int | None = None,
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

    env = os.environ.copy()
    env["EXTRACT_LLM_WORKERS"] = str(DEFAULT_LLM_WORKERS)
    env["EXTRACT_LLM_LOCK_DIR"] = str(DEFAULT_LLM_LOCK_DIR)
    if llm_key_index is not None:
        env["EXTRACT_LLM_KEY_INDEX"] = str(llm_key_index)

    logging.info("Running extraction command: %s", " ".join(command))
    subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        timeout=MAX_EXTRACT_SECONDS,
        env=env,
    )


def assign_next_llm_key_index(llm_workers: int) -> int:
    """Assign a process-wide LLM key/model index to one PDF job.

    The index is global across HTTP requests, so repeated single-PDF requests
    still rotate across configured API keys/models. The chosen index is then
    injected into the extract.py subprocess, which keeps all LLM calls for that
    PDF bound to the same key/model.
    """

    worker_count = max(1, llm_workers)
    DEFAULT_LLM_ASSIGN_DIR.mkdir(parents=True, exist_ok=True)
    counter_path = DEFAULT_LLM_ASSIGN_DIR / "counter.txt"
    lock_path = DEFAULT_LLM_ASSIGN_DIR / "counter.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                counter = int(counter_path.read_text(encoding="utf-8").strip() or "0")
            except (FileNotFoundError, ValueError):
                counter = 0
            assigned = counter % worker_count
            counter_path.write_text(str(counter + 1), encoding="utf-8")
            return assigned
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def api_request_slot():
    """Limit concurrent full parse+extract jobs across API requests/processes."""

    DEFAULT_API_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    slot_file = None
    while slot_file is None:
        for slot_index in range(DEFAULT_API_WORKERS):
            candidate = (DEFAULT_API_LOCK_DIR / f"slot-{slot_index}.lock").open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                candidate.close()
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                continue
            slot_file = candidate
            break
        if slot_file is None:
            time.sleep(DEFAULT_LOCK_POLL_SECONDS)

    try:
        yield
    finally:
        fcntl.flock(slot_file.fileno(), fcntl.LOCK_UN)
        slot_file.close()


if __name__ == "__main__":
    port = int(os.environ.get("EXTRACT_API_PORT", "5002"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
