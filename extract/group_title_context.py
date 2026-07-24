"""收集表格所属章节的标题上下文。

本模块只负责 group 标题，不参与表格筛选、字段判断、封装判断和行提取。
扫描文档时按阅读顺序维护两个窗口：

1. 上一章从章标题到章末出现的全部章节标题；
2. 当前章从章标题到当前扫描位置出现的全部章节标题。

遇到表格时，将“上一章标题 + 当前章已出现标题 + 当前表格标题”按顺序
用换行符连接。表格标题仍由主提取器单独保存，不能用这段上下文替代后
发送给模型的当前表格标题。

当前表格标题只在“上一张表结束到当前表开始”的局部文本窗口内判断：

* 优先使用局部窗口中的 ``Table xxx``/``表 xxx`` 明确表题；
* 没有表号时，允许使用紧邻表格的独立短标题，例如 ``Pin Functions``；
* 局部窗口出现新章节但没有局部表题时，清空上一张表的标题，禁止跨章节
  继承旧表题；
* 局部窗口没有新章节和新表题时，才继承上一张表标题，用于无重复标题的
  跨页续表。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Sequence


_TABLE_TITLE_RE = re.compile(r"^(?:table|表格?|表)\s*\S+", re.IGNORECASE)
_NUMBERED_TABLE_TITLE_RE = re.compile(
    r"^(?:table|表格?|表)\s*"
    r"[\w一二三四五六七八九十百千万]+"
    r"(?:[.\-][\w一二三四五六七八九十百千万]+)*"
    r"\s*[.:：\-–—]?\s*.+$",
    re.IGNORECASE,
)
_FIGURE_TITLE_RE = re.compile(r"^(?:figure|fig\.?|图)\s*\S+", re.IGNORECASE)
_NUMBERED_HEADING_RE = re.compile(
    r"^(?P<number>\d{1,3}(?:\.\d{1,3}){0,5})\s*"
    r"(?:[.:：、]\s*)?(?P<title>\S.*)$"
)
_CHINESE_CHAPTER_RE = re.compile(
    r"^第\s*(?P<number>\d{1,3})\s*章\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_ENGLISH_CHAPTER_RE = re.compile(
    r"^chapter\s+(?P<number>\d{1,3})\b\s*[.:：\-–—]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SectionHeading:
    """一条已经确认的章节标题及其所属一级章节。"""

    text: str
    chapter_number: str
    level: int
    section_number: str = ""


@dataclass
class GroupTitleContextTracker:
    """按阅读顺序维护上一章和当前章的标题。"""

    previous_chapter_titles: list[str] = field(default_factory=list)
    current_chapter_titles: list[str] = field(default_factory=list)
    current_chapter_number: str = ""
    # MinerU 可能把目录条目也输出成 Markdown 标题。目录必须独立保存，
    # 不能直接写入 previous/current 两个正文窗口。
    in_table_of_contents: bool = False
    toc_section_numbers: set[str] = field(default_factory=set)
    toc_chapter_titles: dict[str, list[str]] = field(default_factory=dict)

    def observe(self, value: str, *, require_markdown_heading: bool = False) -> None:
        """读取一行文本；只有确认是章节标题时才更新状态。"""

        cleaned_value = clean_group_title_line(value)
        if _is_table_of_contents_title(cleaned_value):
            self.in_table_of_contents = True
            return

        heading = parse_section_heading(
            value,
            require_markdown_heading=require_markdown_heading,
        )
        if heading is None:
            return

        if self.in_table_of_contents:
            # 目录内同一章可以包含 4、4.1、4.2 等多级条目，因此用完整
            # 章节编号判重。正文再次出现已经登记过的编号时，才结束目录。
            if (
                heading.section_number
                and heading.section_number not in self.toc_section_numbers
            ):
                self.toc_section_numbers.add(heading.section_number)
                toc_title = _strip_toc_page_suffix(heading.text)
                _append_unique(
                    self.toc_chapter_titles.setdefault(
                        heading.chapter_number,
                        [],
                    ),
                    toc_title,
                )
                return
            if heading.section_number in self.toc_section_numbers:
                self.in_table_of_contents = False
            else:
                return

        # 编号标题的第一段数字决定所属一级章节。遇到新的一级章节时，
        # 当前章已经收集完整，可以整体移动到 previous_chapter_titles。
        if (
            heading.chapter_number
            and heading.chapter_number != self.current_chapter_number
        ):
            previous_titles = list(self.current_chapter_titles)
            # 有些文档在目录后直接进入第 3 章，修订历史等第 2 章正文没有
            # 被 MinerU 输出成标题。此时使用目录中紧邻当前章的上一章标题，
            # 不能错误沿用目录扫描结束时的最后一章。
            expected_previous = _previous_chapter_number(heading.chapter_number)
            if expected_previous in self.toc_chapter_titles:
                current_number = _as_int(self.current_chapter_number)
                next_number = _as_int(heading.chapter_number)
                if current_number is None or next_number != current_number + 1:
                    previous_titles = list(
                        self.toc_chapter_titles[expected_previous]
                    )
            self.previous_chapter_titles = previous_titles
            self.current_chapter_titles = []
            self.current_chapter_number = heading.chapter_number

        # 无编号 Markdown 标题只能归入已经明确的当前章节；在文档封面或
        # 前言阶段没有章节编号时不收集，避免污染后续表格 group。
        if not heading.chapter_number and not self.current_chapter_number:
            return

        _append_unique(self.current_chapter_titles, heading.text)

    def build_group_context(self, table_title: str) -> str:
        """生成当前表格的完整 group，上下文之间使用换行分隔。"""

        return join_group_titles(
            *self.previous_chapter_titles,
            *self.current_chapter_titles,
            table_title,
        )


def resolve_table_title(
    values: Sequence[str],
    previous_title: str = "",
) -> str:
    """从当前表之前的局部文本窗口解析表题。

    ``values`` 必须只包含上一张表结束后、当前表开始前的文本。函数先找
    明确的编号表题，再找紧邻表格的独立短标题。只有当前窗口没有出现
    新章节时，才允许回退到 ``previous_title``，从而兼容没有重复标题的
    跨页续表，同时避免把上一章节的表题错误绑定到新章节表格。
    """

    raw_lines = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not raw_lines:
        return clean_group_title_line(previous_title)

    # 编号表题可能与表格之间夹有少量说明文字，因此在整个局部窗口中
    # 反向查找最近一条，而不是只检查最后一行。
    for raw_line in reversed(raw_lines):
        numbered_title = extract_numbered_table_title(raw_line)
        if numbered_title:
            return numbered_title

    # 无表号标题必须紧邻表格。允许跳过常见页眉页脚噪声，但不跨越普通
    # 正文继续向前猜标题，避免把任意短句误当成 group。
    for raw_line in reversed(raw_lines):
        cleaned_line = clean_group_title_line(raw_line)
        if not cleaned_line:
            continue
        if _is_page_noise(cleaned_line):
            continue
        if _looks_like_local_table_title(raw_line):
            return cleaned_line
        break

    # 新章节已经开始时，即使没有独立表题，也不能沿用上一章节的表题。
    if any(
        not _is_page_noise(clean_group_title_line(value))
        and parse_section_heading(value) is not None
        for value in raw_lines
    ):
        return ""
    return clean_group_title_line(previous_title)


def extract_numbered_table_title(value: str) -> str:
    """识别带 Table/表 和表号的明确表题，不限制标题语义关键词。"""

    text = clean_group_title_line(value)
    return text if _NUMBERED_TABLE_TITLE_RE.match(text) else ""


def parse_section_heading(
    value: str,
    *,
    require_markdown_heading: bool = False,
) -> SectionHeading | None:
    """识别 Markdown 标题或常见的中英文编号章节标题。

    Markdown 的 ``#`` 层级只用于确认该行确实是标题；章节归属优先依据
    标题开头的 5、5.1、5.1.2 等编号，因为 MinerU 经常把不同层级标题
    都输出成同一级 ``#``。
    """

    raw = str(value or "").strip()
    markdown_match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", raw)
    if require_markdown_heading and markdown_match is None:
        return None
    markdown_level = len(markdown_match.group(1)) if markdown_match else 0
    text = markdown_match.group(2) if markdown_match else raw
    text = clean_group_title_line(text)
    if not text or len(text) > 240:
        return None
    if _TABLE_TITLE_RE.match(text) or _FIGURE_TITLE_RE.match(text):
        return None

    chinese_match = _CHINESE_CHAPTER_RE.match(text)
    if chinese_match:
        number = chinese_match.group("number")
        return SectionHeading(text, number, 1, number)

    english_match = _ENGLISH_CHAPTER_RE.match(text)
    if english_match:
        number = english_match.group("number")
        return SectionHeading(text, number, 1, number)

    numbered_match = _NUMBERED_HEADING_RE.match(text)
    if numbered_match:
        number = numbered_match.group("number")
        # 非 Markdown 文本必须像标题而不是正文中的编号句子。句末标点通常
        # 表示普通正文；显式 Markdown 标题不受这个限制。
        if not markdown_level and re.search(r"[。！？!?;；]$", text):
            return None
        return SectionHeading(
            text,
            number.split(".", 1)[0],
            number.count(".") + 1,
            number,
        )

    # 无编号标题只有显式 Markdown # 才能确认，后续由 tracker 归入当前章。
    if markdown_level:
        return SectionHeading(text, "", markdown_level)
    return None


def join_group_titles(*values: str) -> str:
    """按输入顺序清理标题、去除重复项并用换行连接。"""

    result: list[str] = []
    for value in values:
        for line in str(value or "").splitlines():
            cleaned = clean_group_title_line(line)
            if cleaned:
                _append_unique(result, cleaned)
    return "\n".join(result)


def append_group_subtitle(group_context: str, subtitle: str) -> str:
    """把表内小分组追加到章节上下文末尾，而不是覆盖完整 group。"""

    return join_group_titles(group_context, subtitle)


def clean_group_title_line(value: str) -> str:
    """清理单条标题，保留标题内容和表号，不吞掉跨标题换行。"""

    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"^\s*#{1,6}\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"\s*[（(]\s*continued\s*[）)]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _looks_like_local_table_title(value: str) -> bool:
    """判断紧邻表格的文本是否像独立标题，而不是正文句子。

    该判断不要求出现 Pin、Signal 等特定词。显式 Markdown 标题直接接受；
    普通文本只接受长度受控、无句末标点、无 URL 且不是章节/图题的短行。
    """

    raw = str(value or "").strip()
    markdown_heading = re.match(r"^\s*#{1,6}\s+\S", raw) is not None
    text = clean_group_title_line(raw)
    if not text or len(text) > 160:
        return False
    if extract_numbered_table_title(text):
        return False
    if _FIGURE_TITLE_RE.match(text) or parse_section_heading(raw) is not None:
        return False
    if markdown_heading:
        return True
    if re.search(r"https?://|www\.", text, flags=re.IGNORECASE):
        return False
    if re.search(r"[。！？!?;；.]$", text):
        return False
    # 普通英文正文通常远长于标题；中文标题没有可靠空格，因此同时保留
    # 总字符长度约束，不按英文单词数直接拒绝中文。
    if re.search(r"[A-Za-z]", text) and len(text.split()) > 18:
        return False
    return True


def _is_page_noise(value: str) -> bool:
    """识别可安全跳过的页眉页脚行，供局部表题查找使用。"""

    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return bool(
        re.search(r"\bcopyright\b|submit documentation feedback|product folder links", normalized)
        or re.fullmatch(r"(?:page\s*)?\d+\s*(?:of\s*\d+)?", normalized)
    )


def _append_unique(target: list[str], value: str) -> None:
    """保留阅读顺序，同一窗口内完全相同的重复标题只记录一次。"""

    if value and value not in target:
        target.append(value)


def _is_table_of_contents_title(value: str) -> bool:
    """识别独立的目录标题，避免把目录条目写进正文章节窗口。"""

    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return normalized in {"table of contents", "contents", "目录", "内容"}


def _strip_toc_page_suffix(value: str) -> str:
    """删除目录标题末尾的点线和页码，保留可读的章节标题。"""

    text = clean_group_title_line(value)
    text = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", text)
    return text.strip()


def _as_int(value: str) -> int | None:
    """把一级章节编号转换为整数；非阿拉伯数字编号不参与跳章推断。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _previous_chapter_number(value: str) -> str:
    """返回当前阿拉伯数字章节的上一章编号。"""

    number = _as_int(value)
    return str(number - 1) if number is not None and number > 1 else ""
