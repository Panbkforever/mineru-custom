"""
Batch parse PDF files with parse_doc.py.

Default layout on AutoDL:
    input PDFs:  /root/autodl-tmp/pdfs/*.pdf
    outputs:     /root/autodl-tmp/outputs/<pdf_stem>/...

Usage:
    python batch_parse.py
    python batch_parse.py -i /root/autodl-tmp/pdfs -o /root/autodl-tmp/outputs
    python batch_parse.py --continue-on-error
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_INPUT_DIR = "/root/autodl-tmp/pdfs"
DEFAULT_OUTPUT_DIR = "/root/autodl-tmp/outputs"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch run parse_doc.py for PDF files under /root/autodl-tmp/pdfs."
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing PDF files. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"MinerU output directory. Default: {DEFAULT_OUTPUT_DIR}",
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
        help="Backend passed to parse_doc.py. Default: hybrid-auto-engine.",
    )
    parser.add_argument(
        "-m",
        "--method",
        default="auto",
        choices=["auto", "txt", "ocr"],
        help="Parse method passed to parse_doc.py. Default: auto.",
    )
    parser.add_argument("--lang", default="ch", help="Document language. Default: ch.")
    parser.add_argument("--start-page", type=int, default=0)
    parser.add_argument("--end-page", type=int, default=None)
    parser.add_argument("--no-formula", action="store_true")
    parser.add_argument("--no-table", action="store_true")
    parser.add_argument("--image-analysis", action="store_true")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Delete each PDF's existing output directory before parsing it.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing remaining PDFs if one parse fails.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not input_dir.is_dir():
        print(f"输入目录不存在: {input_dir}")
        return 1

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"输入目录中没有 PDF: {input_dir}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"输入目录: {input_dir}")
    print(f"输出目录: {output_dir}")
    print(f"解析后端: {args.backend}")
    print(f"待解析 PDF 数量: {len(pdf_files)}")
    print("-" * 60)

    results: list[dict] = []
    failed: list[tuple[Path, int]] = []
    for index, pdf_path in enumerate(pdf_files, start=1):
        print(f"[{index}/{len(pdf_files)}] 解析: {pdf_path.name}")
        if args.clean_output:
            clean_one_output(output_dir, pdf_path.stem)

        command = build_parse_command(project_root, pdf_path, output_dir, args)
        return_code = subprocess.run(command).returncode
        result = collect_result(pdf_path, output_dir, return_code)
        results.append(result)

        if return_code != 0:
            failed.append((pdf_path, return_code))
            print(f"  失败: {pdf_path.name}, return_code={return_code}")
            if not args.continue_on_error:
                break
        else:
            print(f"  完成: {pdf_path.name}")
            print(f"  Markdown: {result.get('markdown_files', [])}")
            print(f"  JSON: {result.get('json_files', [])}")
        print("-" * 60)

    summary_path = output_dir / "batch_parse_summary.json"
    write_summary(results, summary_path)
    print(f"批量解析信息文件: {summary_path}")

    if failed:
        print("失败文件:")
        for pdf_path, return_code in failed:
            print(f"  - {pdf_path.name}: return_code={return_code}")
        return 1

    print("全部解析完成")
    return 0


def build_parse_command(
    project_root: Path,
    pdf_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        sys.executable,
        str(project_root / "parse_doc.py"),
        str(pdf_path),
        "-o",
        str(output_dir),
        "-b",
        args.backend,
        "-m",
        args.method,
        "--lang",
        args.lang,
        "--start-page",
        str(args.start_page),
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
    return command


def clean_one_output(output_dir: Path, pdf_stem: str) -> None:
    target = output_dir / pdf_stem
    if target.exists():
        print(f"  清理旧输出: {target}")
        shutil.rmtree(target)


def collect_result(pdf_path: Path, output_dir: Path, return_code: int) -> dict:
    doc_output_dir = output_dir / pdf_path.stem
    md_files: list[str] = []
    json_files: list[str] = []
    if doc_output_dir.is_dir():
        md_files = [
            str(path.relative_to(output_dir))
            for path in sorted(doc_output_dir.rglob("*.md"))
        ]
        json_files = [
            str(path.relative_to(output_dir))
            for path in sorted(doc_output_dir.rglob(f"{pdf_path.stem}.json"))
        ]

    return {
        "pdf": pdf_path.name,
        "return_code": return_code,
        "output_dir": str(doc_output_dir),
        "markdown_files": md_files,
        "json_files": json_files,
    }


def write_summary(results: list[dict], output_path: Path) -> None:
    payload = {
        "pdf_count": len(results),
        "success_count": sum(1 for item in results if item.get("return_code") == 0),
        "failed_count": sum(1 for item in results if item.get("return_code") != 0),
        "pdf_list": results,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
