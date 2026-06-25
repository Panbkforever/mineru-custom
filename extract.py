"""
Parse documents with parse_doc.py, then extract pin/package fields.

Usage:
    python extract.py input.pdf -o outputs -b hybrid-auto-engine
    python extract.py pdf_dir -o outputs --skip-parse
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from extract.pin_package_extractor import (
    build_extraction_summary,
    extract_pin_package_info_from_middle_json_file,
    strip_debug_fields,
    write_extraction_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MinerU parsing, then extract pin/package fields."
    )
    parser.add_argument("input_path", help="Input PDF/image/Office file or directory.")
    parser.add_argument("-o", "--output", default="./outputs", help="MinerU output dir.")
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
        help="MinerU backend passed to parse_doc.py.",
    )
    parser.add_argument(
        "-m",
        "--method",
        default="auto",
        choices=["auto", "txt", "ocr"],
        help="Parse method passed to parse_doc.py.",
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
        help="Skip parse_doc.py and extract from an existing output dir.",
    )
    parser.add_argument(
        "--extract-output",
        default=None,
        help="Output JSON path. Default: <output>/pin_package_extract.json",
    )
    parser.add_argument(
        "--summary-output",
        default=None,
        help="Output extraction summary JSON path. Default: <extract-output>_info.json",
    )
    parser.add_argument(
        "--semantic-classify",
        action="store_true",
        help="Use DeepSeek semantic classification to filter non-pin tables. Requires DEEPSEEK_API_KEY.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_path)
    output_dir = Path(args.output).resolve()

    if not args.skip_parse:
        run_parse_doc(args, output_dir)

    middle_files = find_middle_json_files(input_path, output_dir)
    if not middle_files:
        print(f"未找到 middle_json: {output_dir}")
        return 1

    extracted_with_debug = []
    for middle_file in middle_files:
        extracted_with_debug.extend(
            extract_pin_package_info_from_middle_json_file(
                middle_file,
                use_semantic_classifier=args.semantic_classify,
                include_debug=True,
            )
        )

    output_path = (
        Path(args.extract_output).resolve()
        if args.extract_output
        else output_dir / "pin_package_extract.json"
    )
    summary_path = (
        Path(args.summary_output).resolve()
        if args.summary_output
        else output_path.with_name(f"{output_path.stem}_info.json")
    )
    extracted = strip_debug_fields(extracted_with_debug)
    pdf_name = input_path.name if input_path.is_file() else input_path.resolve().name
    summary = build_extraction_summary(extracted_with_debug, pdf_name=pdf_name)

    write_extraction_json(extracted, output_path)
    write_extraction_json(summary, summary_path)
    print(f"引脚/封装字段提取完成: {output_path}")
    print(f"提取信息文件完成: {summary_path}")
    print(f"提取 package 数: {len(extracted)}")
    print(json.dumps(extracted[:1], ensure_ascii=False, indent=2))
    return 0


def run_parse_doc(args: argparse.Namespace, output_dir: Path) -> None:
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "parse_doc.py"),
        args.input_path,
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

    subprocess.run(command, check=True)


def find_middle_json_files(input_path: Path, output_dir: Path) -> list[Path]:
    if input_path.is_file():
        candidate_root = output_dir / input_path.stem
        if candidate_root.exists():
            return sorted(candidate_root.rglob("*_middle.json"))
    return sorted(output_dir.rglob("*_middle.json"))


if __name__ == "__main__":
    raise SystemExit(main())
