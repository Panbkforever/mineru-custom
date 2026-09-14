"""
MinerU pin/package extraction API.

Upload multiple PDFs, run the existing parse + extract pipeline for each PDF,
and return a ZIP file containing JSON results and a batch report.

Run:
    cd /root/autodl-tmp
    source .env
    source MinerU/.venv/bin/activate
    python extract_api.py

Example:
    curl -X POST http://localhost:5002/api/extract-pdf-json-batch \
      -F "files=@/root/autodl-tmp/pdfs/a.pdf" \
      -F "files=@/root/autodl-tmp/pdfs/b.pdf" \
      -o results.zip
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
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
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
            "disabled_single_file_endpoint": "/api/extract-pdf-json",
            "method": "POST",
            "form_fields": {
                "files": "one or more PDF files",
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


# Single-PDF request style is intentionally disabled. Keep the implementation
# below for reference/rollback, but do not register it as a Flask route.
# @app.route("/api/extract-pdf-json", methods=["POST"])
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


@app.route("/api/extract-pdf-json-batch", methods=["POST"])
def extract_pdf_json_batch():
    """
    Receive multiple PDFs -> parse/extract each one -> return a ZIP containing:
    - one JSON file per successful PDF
    - _batch_report.json with success/failure details
    """

    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        uploaded_files = request.files.getlist("file")

    uploaded_files = [item for item in uploaded_files if item and item.filename]
    if not uploaded_files:
        return jsonify({"error": "No PDF files selected; use form field 'files'"}), 400

    backend = str_from_request("backend", DEFAULT_BACKEND)
    method = str_from_request("method", DEFAULT_METHOD)
    lang = str_from_request("lang", DEFAULT_LANG)
    semantic_classify = bool_from_request("semantic_classify", DEFAULT_SEMANTIC_CLASSIFY)

    tmpdir = tempfile.mkdtemp(prefix="mineru_extract_api_batch_")
    try:
        tmp_root = Path(tmpdir)
        upload_dir = tmp_root / "uploads"
        results_dir = tmp_root / "results"
        zip_path = tmp_root / "extract_results.zip"
        upload_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        jobs = []
        report = {
            "total": len(uploaded_files),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "api_workers": DEFAULT_API_WORKERS,
            "llm_workers": DEFAULT_LLM_WORKERS,
            "files": [],
        }

        for index, uploaded in enumerate(uploaded_files, start=1):
            original_filename = uploaded.filename or f"uploaded-{index}.pdf"
            filename = secure_filename(original_filename) or f"uploaded-{index}.pdf"
            if not allowed_pdf(filename):
                report["skipped"] += 1
                report["files"].append(
                    {
                        "input": original_filename,
                        "status": "skipped",
                        "error": "Invalid file type, please upload PDF",
                    }
                )
                continue

            safe_stem = make_unique_stem(results_dir, Path(filename).stem, index)
            pdf_path = upload_dir / f"{safe_stem}.pdf"
            parse_output_dir = tmp_root / "mineru_output" / safe_stem
            extract_output = results_dir / f"{safe_stem}.json"
            summary_output = results_dir / f"{safe_stem}_info.json"
            uploaded.save(pdf_path)

            jobs.append(
                {
                    "input": original_filename,
                    "filename": filename,
                    "safe_stem": safe_stem,
                    "pdf_path": pdf_path,
                    "parse_output_dir": parse_output_dir,
                    "extract_output": extract_output,
                    "summary_output": summary_output,
                    "llm_key_index": fixed_llm_key_index(index, DEFAULT_LLM_WORKERS),
                }
            )

        logging.info(
            "Received batch: %s files, %s valid PDFs, api_workers=%s, llm_workers=%s",
            len(uploaded_files),
            len(jobs),
            DEFAULT_API_WORKERS,
            DEFAULT_LLM_WORKERS,
        )

        if not jobs:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return jsonify(report), 400

        max_workers = min(DEFAULT_API_WORKERS, len(jobs))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_job = {
                executor.submit(
                    run_one_batch_job,
                    job,
                    backend,
                    method,
                    lang,
                    semantic_classify,
                ): job
                for job in jobs
            }
            for future in as_completed(future_to_job):
                job = future_to_job[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logging.exception("Batch extraction failed for %s", job["input"])
                    result = {
                        "input": job["input"],
                        "output": f"{job['safe_stem']}.json",
                        "status": "failed",
                        "llm_key_index": job.get("llm_key_index"),
                        "error": str(exc),
                    }

                if result.get("status") == "success":
                    report["success"] += 1
                else:
                    report["failed"] += 1
                report["files"].append(result)

        report["files"].sort(key=lambda item: str(item.get("input", "")))
        report_path = results_dir / "_batch_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(report_path, arcname="_batch_report.json")
            for item in report["files"]:
                if item.get("status") != "success":
                    continue
                output_path = results_dir / str(item["output"])
                if output_path.exists():
                    archive.write(output_path, arcname=output_path.name)

        @after_this_request
        def cleanup(response):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return response

        response = send_file(
            zip_path,
            as_attachment=True,
            download_name="extract_results.zip",
            mimetype="application/zip",
        )
        if report["failed"] or report["skipped"]:
            response.status_code = 207
        return response

    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logging.exception("Batch PDF extraction API failed")
        return jsonify(
            {
                "error": "Batch PDF extraction API failed",
                "detail": str(exc),
            }
        ), 500


def make_unique_stem(results_dir: Path, stem: str, index: int) -> str:
    safe_stem = secure_filename(stem) or f"uploaded-{index}"
    candidate = f"{index:03d}_{safe_stem}"
    suffix = 2
    while (results_dir / f"{candidate}.json").exists():
        candidate = f"{index:03d}_{safe_stem}_{suffix}"
        suffix += 1
    return candidate


def run_one_batch_job(
    job: dict,
    backend: str,
    method: str,
    lang: str,
    semantic_classify: bool,
) -> dict:
    logging.info("Batch extraction started: %s", job["input"])
    try:
        with api_request_slot():
            run_extract_pipeline(
                pdf_path=job["pdf_path"],
                parse_output_dir=job["parse_output_dir"],
                extract_output=job["extract_output"],
                summary_output=job["summary_output"],
                backend=backend,
                method=method,
                lang=lang,
                semantic_classify=semantic_classify,
                llm_key_index=job["llm_key_index"],
            )

        if not job["extract_output"].exists():
            raise FileNotFoundError(f"Extraction JSON not found: {job['extract_output']}")

        logging.info("Batch extraction finished: %s", job["input"])
        return {
            "input": job["input"],
            "output": job["extract_output"].name,
            "status": "success",
            "llm_key_index": job["llm_key_index"],
        }
    except subprocess.TimeoutExpired as exc:
        logging.exception("Batch extraction timed out for %s", job["input"])
        return {
            "input": job["input"],
            "output": job["extract_output"].name,
            "status": "failed",
            "llm_key_index": job.get("llm_key_index"),
            "error": "Extraction timed out",
            "detail": str(exc),
        }
    except subprocess.CalledProcessError as exc:
        logging.exception("Batch extraction command failed for %s", job["input"])
        return {
            "input": job["input"],
            "output": job["extract_output"].name,
            "status": "failed",
            "llm_key_index": job.get("llm_key_index"),
            "error": "Extraction failed",
            "return_code": exc.returncode,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except Exception as exc:
        logging.exception("Batch extraction failed for %s", job["input"])
        return {
            "input": job["input"],
            "output": job["extract_output"].name,
            "status": "failed",
            "llm_key_index": job.get("llm_key_index"),
            "error": str(exc),
        }


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


def fixed_llm_key_index(pdf_index: int, llm_workers: int) -> int:
    """Bind one PDF job to one LLM key/model index for its whole subprocess."""

    return (max(1, pdf_index) - 1) % max(1, llm_workers)


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
