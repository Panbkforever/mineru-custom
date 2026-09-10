"""
Batch parse PDFs and extract pin/package fields.

Default layout:
    input PDFs:      ./Multi_package_TIpdf/*.pdf
    extracted JSON:  ./ex_outputs/<pdf_stem>.json
    MinerU outputs:  ./ex_outputs/_mineru_parse/<pdf_stem>/...

Usage:
    python batch_extract.py
    python batch_extract.py --skip-parse
    python batch_extract.py -i Multi_package_TIpdf -o ex_outputs -b hybrid-auto-engine
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch run extract.py for PDFs under Multi_package_TIpdf."
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        default="Multi_package_TIpdf",
        help="Directory containing PDF files. Default: ./Multi_package_TIpdf",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="ex_outputs",
        help="Directory for one JSON per PDF. Default: ./ex_outputs",
    )
    parser.add_argument(
        "--parse-output-dir",
        default=None,
        help="MinerU parse output dir. Default: <output-dir>/_mineru_parse",
    )
    parser.add_argument(
        "-b",
        "--backend",
        default="hybrid-auto-engine",
        choices=[
            "pipeline",
            "vlm-auto-engine",
            "hybrid-auto-engine",
            "vlm-http-client",
            "hybrid-http-client",
        ],
        help="Backend passed to extract.py/parse_doc.py.",
    )
    parser.add_argument(
        "-m",
        "--method",
        default="auto",
        choices=["auto", "txt", "ocr"],
        help="Parse method passed to extract.py/parse_doc.py.",
    )
    parser.add_argument("--lang", default="ch", help="Document language.")
    parser.add_argument("--start-page", type=int, default=0)
    parser.add_argument("--end-page", type=int, default=None)
    parser.add_argument("--no-formula", action="store_true")
    parser.add_argument("--no-table", action="store_true")
    parser.add_argument("--image-analysis", action="store_true")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument(
        "--skip-parse",
        action="store_true",
        help="Reuse existing MinerU outputs and only run extraction.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing remaining PDFs if one file fails.",
    )
    parser.add_argument(
        "--semantic-classify",
        action="store_true",
        help="Use DeepSeek semantic classification in extract.py. Requires DEEPSEEK_API_KEY.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of PDFs to process concurrently. Default: 1.",
    )
    parser.add_argument(
        "--llm-workers",
        type=int,
        default=1,
        help=(
            "Global DeepSeek request concurrency shared by all PDF workers. "
            "Default: 1."
        ),
    )
    args = parser.parse_args()
    if args.workers < 1:
        print("--workers 必须 >= 1")
        return 1
    if args.llm_workers < 1:
        print("--llm-workers 必须 >= 1")
        return 1

    project_root = Path(__file__).resolve().parent
    input_dir = (project_root / args.input_dir).resolve()
    output_dir = (project_root / args.output_dir).resolve()
    parse_output_dir = (
        Path(args.parse_output_dir).resolve()
        if args.parse_output_dir
        else output_dir / "_mineru_parse"
    )

    if not input_dir.is_dir():
        print(f"输入目录不存在: {input_dir}")
        return 1

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"输入目录中没有 PDF: {input_dir}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    parse_output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = output_dir / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    llm_lock_dir = output_dir / "_llm_locks"
    llm_lock_dir.mkdir(parents=True, exist_ok=True)

    print(f"输入目录: {input_dir}")
    print(f"JSON 输出目录: {output_dir}")
    print(f"MinerU 输出目录: {parse_output_dir}")
    print(f"待处理 PDF 数量: {len(pdf_files)}")
    print(f"PDF 并发数: {args.workers}")
    print(f"DeepSeek 全局并发数: {args.llm_workers}")
    print(f"日志目录: {log_dir}")
    print("-" * 60)

    failed: list[tuple[Path, int]] = []
    batch_summaries: list[dict] = []
    worker_results = run_batch_extract_jobs(
        pdf_files=pdf_files,
        project_root=project_root,
        parse_output_dir=parse_output_dir,
        output_dir=output_dir,
        log_dir=log_dir,
        llm_lock_dir=llm_lock_dir,
        args=args,
    )
    for result in sorted(worker_results, key=lambda item: item["index"]):
        pdf_path = Path(result["pdf_path"])
        return_code = int(result["return_code"])
        if return_code != 0:
            failed.append((pdf_path, return_code))
        elif result.get("summary"):
            batch_summaries.append(result["summary"])

    batch_summary_output = output_dir / "extraction_summary.json"
    write_batch_summary(batch_summaries, batch_summary_output)
    print(f"批量信息文件: {batch_summary_output}")

    if failed:
        print("失败文件:")
        for pdf_path, return_code in failed:
            print(f"  - {pdf_path.name}: return_code={return_code}")
        return 1

    print("全部处理完成")
    return 0


def run_batch_extract_jobs(
    *,
    pdf_files: list[Path],
    project_root: Path,
    parse_output_dir: Path,
    output_dir: Path,
    log_dir: Path,
    llm_lock_dir: Path,
    args: argparse.Namespace,
) -> list[dict]:
    results: list[dict] = []
    next_index = 0
    stop_submitting = False
    active = {}

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal next_index
        if next_index >= len(pdf_files):
            return False
        index = next_index + 1
        pdf_path = pdf_files[next_index]
        next_index += 1
        print(f"[{index}/{len(pdf_files)}] 开始处理: {pdf_path.name}")
        future = executor.submit(
            run_one_extract_job,
            index=index,
            total=len(pdf_files),
            pdf_path=pdf_path,
            project_root=project_root,
            parse_output_dir=parse_output_dir,
            output_dir=output_dir,
            log_dir=log_dir,
            llm_lock_dir=llm_lock_dir,
            args=args,
        )
        active[future] = pdf_path
        return True

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for _ in range(min(args.workers, len(pdf_files))):
            submit_next(executor)

        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                pdf_path = active.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    result = {
                        "index": pdf_files.index(pdf_path) + 1,
                        "pdf_path": str(pdf_path),
                        "return_code": 1,
                        "log_path": "",
                        "error": str(exc),
                    }
                results.append(result)
                return_code = int(result["return_code"])
                if return_code == 0:
                    print(
                        f"[{result['index']}/{len(pdf_files)}] 完成: {pdf_path.name}"
                    )
                    print(f"  输出: {result['json_output']}")
                    print(f"  信息: {result['summary_output']}")
                    print(f"  日志: {result['log_path']}")
                else:
                    print(
                        f"[{result['index']}/{len(pdf_files)}] 失败: "
                        f"{pdf_path.name}, return_code={return_code}"
                    )
                    if result.get("error"):
                        print(f"  错误: {result['error']}")
                    print(f"  日志: {result.get('log_path') or '无'}")
                    if not args.continue_on_error:
                        stop_submitting = True
                print("-" * 60)

            while not stop_submitting and len(active) < args.workers:
                if not submit_next(executor):
                    break

    return results


def run_one_extract_job(
    *,
    index: int,
    total: int,
    pdf_path: Path,
    project_root: Path,
    parse_output_dir: Path,
    output_dir: Path,
    log_dir: Path,
    llm_lock_dir: Path,
    args: argparse.Namespace,
) -> dict:
    json_output = output_dir / f"{pdf_path.stem}.json"
    summary_output = output_dir / f"{pdf_path.stem}_info.json"
    log_path = log_dir / f"{pdf_path.stem}.log"
    command = build_extract_command(
        project_root=project_root,
        pdf_path=pdf_path,
        parse_output_dir=parse_output_dir,
        json_output=json_output,
        summary_output=summary_output,
        args=args,
    )
    env = os.environ.copy()
    env["EXTRACT_LLM_WORKERS"] = str(args.llm_workers)
    env["EXTRACT_LLM_LOCK_DIR"] = str(llm_lock_dir)

    with log_path.open("w", encoding="utf-8") as log_file:
        print(f"[{index}/{total}] PDF: {pdf_path.name}", file=log_file)
        print("COMMAND:", " ".join(command), file=log_file)
        print(f"EXTRACT_LLM_WORKERS={args.llm_workers}", file=log_file)
        print(f"EXTRACT_LLM_LOCK_DIR={llm_lock_dir}", file=log_file)
        print("-" * 60, file=log_file)
        log_file.flush()
        completed = subprocess.run(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )

    summary = None
    error = ""
    if completed.returncode == 0 and summary_output.exists():
        try:
            summary = json.loads(summary_output.read_text(encoding="utf-8"))
        except Exception as exc:
            error = f"读取 summary 失败: {exc}"

    return {
        "index": index,
        "pdf_path": str(pdf_path),
        "return_code": completed.returncode,
        "json_output": str(json_output),
        "summary_output": str(summary_output),
        "log_path": str(log_path),
        "summary": summary,
        "error": error,
    }


def build_extract_command(
    project_root: Path,
    pdf_path: Path,
    parse_output_dir: Path,
    json_output: Path,
    summary_output: Path,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        sys.executable,
        str(project_root / "extract.py"),
        str(pdf_path),
        "-o",
        str(parse_output_dir),
        "-b",
        args.backend,
        "-m",
        args.method,
        "--lang",
        args.lang,
        "--start-page",
        str(args.start_page),
        "--extract-output",
        str(json_output),
        "--summary-output",
        str(summary_output),
    ]
    if args.end_page is not None:
        command.extend(["--end-page", str(args.end_page)])
    if args.no_formula:
        command.append("--no-formula")
    if args.no_table:
        command.append("--no-table")
    if args.image_analysis:
        command.append("--image-analysis")
    if args.no_images:
        command.append("--no-images")
    if args.skip_parse:
        command.append("--skip-parse")
    if args.semantic_classify:
        command.append("--semantic-classify")
    return command


def write_batch_summary(summaries: list[dict], output_path: Path) -> None:
    total_pins = sum(int(summary.get("pin_count", 0)) for summary in summaries)
    total_packages = sum(int(summary.get("package_count", 0)) for summary in summaries)
    payload = {
        "pdf_count": len(summaries),
        "package_count": total_packages,
        "pin_count": total_pins,
        "pdf_list": summaries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
