"""接收单个 PDF，返回图片信息 JSON 与 images 目录组成的 ZIP。"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

from flask import Flask, Response, jsonify, request
import psutil
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "mineru_image_export" / "export_images.py"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("环境变量 %s 无效，使用默认值 %s", name, default)
    return default


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = positive_env("IMAGE_API_MAX_MB", 200) * 1024 * 1024
WORKERS = positive_env("IMAGE_API_WORKERS", 1)
TIMEOUT = positive_env("IMAGE_API_TIMEOUT", 3600)
QUEUE_TIMEOUT = positive_env("IMAGE_API_QUEUE_TIMEOUT", 300)
LOCK_DIR = Path(os.environ.get("IMAGE_API_LOCK_DIR", str(ROOT / "mineru_image_export" / "api_locks")))
BACKENDS = {"pipeline", "vlm-auto-engine", "hybrid-auto-engine"}
METHODS = {"auto", "txt", "ocr"}
LANGUAGES = {"ch", "ch_server", "ch_lite", "en", "korean", "japan", "chinese_cht", "ta", "te", "ka",
             "th", "el", "latin", "arabic", "east_slavic", "cyrillic", "devanagari"}


@contextlib.contextmanager
def request_slot():
    """通过文件锁限制本接口跨线程、跨进程的 PDF 任务数量。"""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + QUEUE_TIMEOUT
    lock = None
    while lock is None:
        for index in range(WORKERS):
            candidate = (LOCK_DIR / f"slot-{index}.lock").open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                candidate.close()
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
            else:
                lock = candidate
                break
        if lock is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("任务排队超时")
            time.sleep(.2)
    try:
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def stop_process_tree(process):
    """优先允许 MinerU 自行收尾，再清理跨进程组的残留子进程。"""
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=5)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=5)


def run_pipeline(pdf: Path, output: Path, workspace: Path, backend: str, method: str, lang: str):
    if not TOOL.is_file():
        raise FileNotFoundError("独立图片导出工具不存在")
    command = [os.environ.get("IMAGE_API_PYTHON", sys.executable), str(TOOL),
               "-i", str(pdf), "-o", str(output), "--parse-output-dir", str(workspace / "parsed"),
               "-b", backend, "-m", method, "--lang", lang]
    # 日志落入请求独立目录，避免长文档的大量输出占满服务进程内存。
    with (workspace / "pipeline.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=TIMEOUT)
        except BaseException:
            stop_process_tree(process)
            raise
    if code:
        with (workspace / "pipeline.log").open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 16000))
            detail = log.read().decode("utf-8", errors="replace")
        logger.error("图片导出子进程失败，退出码 %s，日志末尾：\n%s", code, detail)
        raise subprocess.CalledProcessError(code, command)


def build_archive(metadata: Path, archive: Path, workspace: Path, stem: str):
    """仅打包导出的图片与一个 JSON，并重写为 ZIP 内可直接使用的相对路径。"""
    records = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("图片信息 JSON 必须是列表")
    paths = {}
    names = set()
    rewritten = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"image_path", "image_title", "section"}:
            raise ValueError("图片记录字段不完整")
        if not all(isinstance(value, str) for value in record.values()) or not record["image_path"]:
            raise ValueError("图片记录字段必须是字符串且路径不能为空")
        relative = Path(record["image_path"])
        if relative.is_absolute():
            raise ValueError("图片路径必须是相对路径")
        asset = (metadata.parent / relative).resolve()
        # 只能打包当前请求内生成的文件，不能借图片路径读走服务器其他文件。
        if not asset.is_relative_to((workspace / "parsed").resolve()) or not asset.is_file():
            raise ValueError("图片文件缺失或越出请求目录")
        if asset.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".svg"}:
            raise ValueError("打包目标不是支持的图片文件")
        if asset not in paths:
            name = secure_filename(asset.name) or "image"
            base = Path(name)
            index = 2
            while name.casefold() in names:
                name = f"{base.stem}_{index}{base.suffix}"
                index += 1
            names.add(name.casefold())
            paths[asset] = "images/" + name
        rewritten.append({**record, "image_path": paths[asset]})
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zipped:
        zipped.writestr("images/", b"")
        zipped.writestr(stem + "_images.json", json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n")
        for asset, destination in paths.items():
            zipped.write(asset, destination)


def archive_response(archive: Path, workspace: Path, filename: str) -> Response:
    handle = archive.open("rb")

    def cleanup():
        handle.close()
        shutil.rmtree(workspace, ignore_errors=True)

    def chunks():
        try:
            while chunk := handle.read(1024 * 1024):
                yield chunk
        finally:
            cleanup()

    response = Response(chunks(), mimetype="application/zip")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    response.content_length = archive.stat().st_size
    # 正常发送完成或客户端提前关闭都清理，不在响应开始前删除 ZIP。
    response.call_on_close(cleanup)
    return response


@app.get("/")
def index():
    return jsonify({"endpoint": "/api/extract-pdf-images-zip", "method": "POST",
                    "file_fields": ["files", "file"], "files_per_request": 1,
                    "optional_fields": {"backend": sorted(BACKENDS), "method": sorted(METHODS), "lang": sorted(LANGUAGES)},
                    "response": "ZIP：一个 *_images.json 和 images/ 目录"})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "tool_available": TOOL.is_file(), "api_workers": WORKERS})


@app.errorhandler(413)
def too_large(error):
    return jsonify({"error": "上传文件超过大小限制"}), 413


@app.post("/api/extract-pdf-images-zip")
def extract_images():
    files = [item for name in ("files", "file") for item in request.files.getlist(name) if item.filename]
    if len(files) != 1:
        return jsonify({"error": "每次请求必须上传一个 PDF，使用 files 或 file 字段", "received": len(files)}), 400
    upload = files[0]
    original = (upload.filename or "").replace("\\", "/")
    if Path(original).suffix.lower() != ".pdf":
        return jsonify({"error": "只支持 PDF 文件"}), 400
    stem = secure_filename(Path(original).stem) or "document"
    backend = request.form.get("backend", "hybrid-auto-engine").strip()
    method = request.form.get("method", "auto").strip()
    lang = request.form.get("lang", "ch").strip()
    if backend not in BACKENDS or method not in METHODS or lang not in LANGUAGES:
        return jsonify({"error": "backend、method 或 lang 参数不支持"}), 400
    workspace = Path(tempfile.mkdtemp(prefix="mineru_image_api_"))
    handed_off = False
    try:
        pdf = workspace / (stem + ".pdf")
        metadata = workspace / "metadata.json"
        archive = workspace / "result.zip"
        upload.save(pdf)
        with pdf.open("rb") as handle:
            if b"%PDF-" not in handle.read(1024):
                return jsonify({"error": "文件内容不是有效的 PDF 文件头"}), 400
        with request_slot():
            run_pipeline(pdf, metadata, workspace, backend, method, lang)
            build_archive(metadata, archive, workspace, stem)
        response = archive_response(archive, workspace, stem + "_images.zip")
        handed_off = True
        return response
    except subprocess.TimeoutExpired:
        logger.exception("PDF 图片导出超时")
        return jsonify({"error": "PDF 解析和图片导出超时"}), 504
    except TimeoutError:
        return jsonify({"error": "任务排队超时，请稍后重试"}), 503, {"Retry-After": "10"}
    except Exception:
        logger.exception("PDF 图片导出失败")
        return jsonify({"error": "PDF 图片导出失败，请查看服务日志"}), 500
    finally:
        if not handed_off:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    app.run(host=os.environ.get("IMAGE_API_HOST", "0.0.0.0"),
            port=positive_env("IMAGE_API_PORT", 5003), debug=False, threaded=True)
