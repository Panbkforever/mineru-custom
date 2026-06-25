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
import subprocess
import sys
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
    args = parser.parse_args()

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

    print(f"输入目录: {input_dir}")
    print(f"JSON 输出目录: {output_dir}")
    print(f"MinerU 输出目录: {parse_output_dir}")
    print(f"待处理 PDF 数量: {len(pdf_files)}")
    print("-" * 60)

    failed: list[tuple[Path, int]] = []
    batch_summaries: list[dict] = []
    for index, pdf_path in enumerate(pdf_files, start=1):
        json_output = output_dir / f"{pdf_path.stem}.json"
        summary_output = output_dir / f"{pdf_path.stem}_info.json"
        print(f"[{index}/{len(pdf_files)}] 处理: {pdf_path.name}")

        command = build_extract_command(
            project_root=project_root,
            pdf_path=pdf_path,
            parse_output_dir=parse_output_dir,
            json_output=json_output,
            summary_output=summary_output,
            args=args,
        )
        return_code = subprocess.run(command).returncode
        if return_code != 0:
            failed.append((pdf_path, return_code))
            print(f"  失败: {pdf_path.name}, return_code={return_code}")
            if not args.continue_on_error:
                break
        else:
            print(f"  输出: {json_output}")
            print(f"  信息: {summary_output}")
            if summary_output.exists():
                batch_summaries.append(json.loads(summary_output.read_text(encoding="utf-8")))
        print("-" * 60)

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
