"""独立处理 PDF 或已有 MinerU 结果，导出图片路径、图注和完整章节。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterator

from errors import ExportError


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"br", "p", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in {"p", "div"}:
            self.parts.append(" ")


def text(value: Any) -> str:
    """读取字符串或 v2 富文本，不把图片内容、脚注混入标题。"""
    if value is None:
        return ""
    if isinstance(value, list):
        # v2 的富文本片段保留片段自带空格，不能擅自拆开带格式的单词。
        return "".join(text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") not in {None, "text", "inline_math", "interline_math"}:
            return ""
        return text(value.get("content", ""))
    return str(value)


def clean(value: str) -> str:
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()


def caption(value: Any) -> str:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return clean(" ".join(value))
    return clean(text(value))


@dataclass(frozen=True)
class Event:
    kind: str
    title: str = ""
    level: int = 1
    path: str = ""


def heading_level(value: Any) -> int:
    # bool 是 int 的子类，这里不能把 true 当作有效标题级别。
    return value if type(value) is int and value > 0 else 1


def _v1_events(items: list) -> Iterator[Event]:
    for item in items:
        if not isinstance(item, dict) or "type" not in item:
            raise ExportError("content_list 中存在无效条目，必须是含 type 的对象。")
        kind = item["type"]
        if kind in {"text", "title"} and (item.get("text_level") or kind == "title"):
            yield Event("heading", clean(text(item.get("text", ""))), heading_level(item.get("text_level")))
        elif kind == "image":
            yield Event("image", caption(item.get("image_caption", [])), path=item.get("img_path", ""))


def _v2_events(pages: list) -> Iterator[Event]:
    for page in pages:
        if not isinstance(page, list):
            raise ExportError("v2 content_list 必须是按页组织的条目列表。")
        for item in page:
            if not isinstance(item, dict) or "type" not in item:
                raise ExportError("v2 content_list 中存在无效条目。")
            content = item.get("content", {})
            if item["type"] in {"title", "image"} and not isinstance(content, dict):
                raise ExportError("v2 标题或图片的 content 必须是对象。")
            if item["type"] == "title":
                yield Event("heading", clean(text(content.get("title_content", []))), heading_level(content.get("level")))
            elif item["type"] == "image":
                source = content.get("image_source", {})
                if not isinstance(source, dict):
                    raise ExportError("v2 图片的 image_source 必须是对象。")
                yield Event("image", caption(content.get("image_caption", [])), path=source.get("path", ""))


def events(data: Any) -> Iterator[Event]:
    if not isinstance(data, list):
        raise ExportError("仅支持 MinerU content_list.json 或 content_list_v2.json，不接受模型结果或任意 JSON。")
    if not data:
        return
    if isinstance(data[0], list):
        yield from _v2_events(data)
    else:
        yield from _v1_events(data)


def section_number(title: str) -> tuple[str, ...] | None:
    # 编号只用于确定层级；祖先章节名称必须真实出现在已读标题中。
    match = re.match(r"^(\d+(?:\.\d+)*)(?:[.)]?\s+)(?=\S)", title)
    if match:
        return tuple(match[1].split("."))
    match = re.match(r"^(?:Appendix\s+)?([A-Z](?:\.\d+)*)(?:[.)]?\s+)(?=\S)", title)
    if match:
        return tuple(match[1].split("."))
    return None


class Sections:
    def __init__(self):
        self.stack: list[tuple[int, str]] = []
        self.numbered: dict[tuple[str, ...], str] = {}

    def add(self, title: str, level: int):
        if not title:
            return
        number = section_number(title)
        if number:
            self.numbered[number] = title
            # 明确编号优先于 MinerU 常见的全一级标注；不凭空构造缺失的父标题。
            self.stack = [(depth, self.numbered[number[:depth]]) for depth in range(1, len(number) + 1)
                          if number[:depth] in self.numbered]
        else:
            self.stack = [(depth, label) for depth, label in self.stack if depth < level]
            self.stack.append((level, title))

    def current(self) -> str:
        return " > ".join(label for _, label in self.stack)


def find_source(path: Path) -> Path:
    if path.is_file():
        return path.resolve()
    if not path.is_dir():
        raise ExportError(f"输入路径不存在：{path}")
    # 同一份结果优先使用 v1，避免 v1/v2 同时读取而产生重复图片。
    sources: dict[tuple[Path, str], Path] = {}
    for candidate in sorted(path.rglob("*_content_list*.json")):
        if candidate.name.endswith("_content_list_v2.json"):
            stem = candidate.name[:-len("_content_list_v2.json")]
        elif candidate.name.endswith("_content_list.json"):
            stem = candidate.name[:-len("_content_list.json")]
        else:
            continue
        key = (candidate.parent.resolve(), stem)
        if key not in sources or candidate.name.endswith("_content_list.json"):
            sources[key] = candidate
    if len(sources) != 1:
        raise ExportError(f"目录中找到 {len(sources)} 份解析结果，请指定一个 PDF 的结果目录或具体 content_list 文件。")
    return next(iter(sources.values())).resolve()


def source_stem(path: Path) -> str:
    for suffix in ("_content_list_v2.json", "_content_list.json"):
        if path.name.endswith(suffix):
            return path.name[:-len(suffix)]
    return path.stem


def collect(source: Path, output: Path) -> list[dict[str, str]]:
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ExportError("输出路径不能覆盖输入 JSON。")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"无法读取输入 JSON：{exc}") from exc
    sections = Sections()
    records = []
    for event in events(data):
        if event.kind == "heading":
            sections.add(event.title, event.level)
            continue
        if not isinstance(event.path, str) or not event.path.strip():
            raise ExportError("图片条目缺少有效图片路径，未生成不完整清单。")
        asset = Path(event.path.replace("\\", "/"))
        if not asset.is_absolute():
            asset = source.parent / asset
        asset = asset.resolve()
        if asset == output:
            raise ExportError("输出路径不能覆盖图片文件。")
        if not asset.is_file():
            raise ExportError(f"图片不存在：{asset}；请保留解析结果配套的 images 目录。")
        records.append({"image_path": Path(os.path.relpath(asset, output.parent)).as_posix(),
                        "image_title": event.title, "section": sections.current()})
    return records


def export(source: Path, output: Path, overwrite: bool = False) -> list[dict[str, str]]:
    if output.exists() and not overwrite:
        raise ExportError(f"输出已存在：{output}；如需覆盖请传 --overwrite。")
    records = collect(source, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # 先完成全部验证，再原子写入，避免中途出错留下半份 JSON。
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".images-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="输入 PDF，先调用 MinerU 解析，再导出图片路径、图注和完整章节。")
    parser.add_argument("-i", "--input", required=True, type=Path, help="PDF 文件；也兼容 content_list JSON 或单个 PDF 的解析结果目录")
    parser.add_argument("-o", "--output", type=Path, help="输出 JSON；默认放到本工具 exports 目录")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出，不允许覆盖输入或图片")
    parser.add_argument("--parse-output-dir", type=Path, default=Path(__file__).resolve().parent / "parsed",
                        help="PDF 解析结果根目录，默认本工具 parsed 目录，每次运行独立保存")
    parser.add_argument("-b", "--backend", choices=["pipeline", "vlm-auto-engine", "hybrid-auto-engine"],
                        default="hybrid-auto-engine", help="MinerU 本地解析后端，仅 PDF 输入使用")
    parser.add_argument("-m", "--method", choices=["auto", "txt", "ocr"], default="auto", help="PDF 解析方式")
    parser.add_argument("--lang", default="ch", help="MinerU OCR 语言，例如 ch、en")
    parser.add_argument("--mineru-python", help="安装 MinerU 的 Python 解释器路径，默认使用当前 Python")
    args = parser.parse_args(argv)
    try:
        if args.input.suffix.lower() == ".pdf":
            # 在启动昂贵的 PDF 解析前检查输出，避免已存在的结果导致白跑。
            output = args.output or Path(__file__).resolve().parent / "exports" / (args.input.stem + "_images.json")
            output = output.resolve()
            if output == args.input.resolve():
                raise ExportError("输出路径不能覆盖输入 PDF。")
            if output.exists() and not args.overwrite:
                raise ExportError(f"输出已存在：{output}；如需覆盖请传 --overwrite。")
            if output.exists() and not output.is_file():
                raise ExportError(f"输出路径不是普通文件：{output}")
            if not output.parent.exists():
                output.parent.mkdir(parents=True, exist_ok=True)
            from pdf_input import parse_pdf

            run_dir = parse_pdf(args.input, args.parse_output_dir, backend=args.backend, method=args.method,
                                lang=args.lang, python=args.mineru_python)
            source = find_source(run_dir)
        else:
            source = find_source(args.input)
            output = args.output or Path(__file__).resolve().parent / "exports" / (source_stem(source) + "_images.json")
        records = export(source, output.resolve(), args.overwrite)
    except (OSError, ExportError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已中断，未完成图片信息导出。", file=sys.stderr)
        return 130
    print(f"已导出 {len(records)} 张图片：{output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
