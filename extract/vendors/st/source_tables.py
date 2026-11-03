"""ST 物理引脚表的源表头修复，不改写数据行。"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
import re

from bs4 import BeautifulSoup


def _text(cell):
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def _key(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _cells(row):
    return row.find_all(["td", "th"], recursive=False)


def _package(value):
    return bool(re.search(r"\b(?:[a-z]*qfp|lofp|[a-z]*bga|wlcsp|sot|flip\s*[- ]?\s*chip)[i\s-]*\d", value, re.I))


def _ball_header(candidate):
    if not re.search(r"\bpin\s*/\s*ball\s+definition", candidate.title, re.I):
        return None
    soup = BeautifulSoup(candidate.html, "html.parser")
    rows = soup.find_all("tr")
    if len(rows) < 3:
        return None
    first, second = _cells(rows[0]), _cells(rows[1])
    top_count = 0
    for cell in first:
        if not _key(_text(cell)).startswith("pinballname"):
            break
        top_count += 1
    labels = []
    for cell in second:
        value = _text(cell)
        if not _package(value):
            break
        labels.append(value)
    if top_count < 2 or len(labels) < 2:
        return None
    return soup, rows, first, second, labels, top_count


def _compatible(left, right):
    a, b = _key(left), _key(right)
    if ('withsmps' in a) != ('withsmps' in b):
        return False
    def variants(value):
        value = re.sub(r"^lofp(?=\d)", "lqfp", value)
        return {value, re.sub(r"(wlcsp)i(?=\d)", r"\g<1>1", value),
                re.sub(r"(wlcsp)i(?=\d)", r"\g<1>", value)}
    return bool(variants(a) & variants(b))


def _aligned(rows, count):
    # 只用已有单元格验证列位置，绝不据此删选数据行。
    hits = 0
    for row in rows[2:12]:
        cells = _cells(row)
        if len(cells) <= count + 1:
            continue
        name, kind = _text(cells[count]), _text(cells[count + 1])
        if name and re.fullmatch(r"(?:I/O|I|O|S|A|ANA|P|Power|Supply)(?:\s*\([^)]*\))?", kind, re.I):
            hits += 1
    return hits >= min(2, len(rows) - 2)


def _split_ball_header_sections(candidate):
    parsed = _ball_header(candidate)
    if parsed is None:
        return [candidate]
    soup, rows, _, _, original_labels, _ = parsed
    # 有跨行单元格时无法仅靠行边界无损切分，保留原表交给原流程。
    if any(int(cell.get("rowspan", 1) or 1) > 1
           for row in rows for cell in _cells(row)
           if str(cell.get("rowspan", 1) or 1).isdigit()):
        return [candidate]
    boundaries = [0]
    for index in range(2, len(rows) - 1):
        top = _cells(rows[index])
        second = _cells(rows[index + 1])
        count = 0
        for cell in top:
            if not _key(_text(cell)).startswith("pinballname"):
                break
            count += 1
        labels = []
        for cell in second:
            label = _text(cell)
            if not _package(label):
                break
            labels.append(label)
        if (count >= 2 and len(labels) >= 2
                and abs(len(labels) - len(original_labels)) <= 1
                and all(_compatible(a, b) for a, b in zip(labels, original_labels))):
            boundaries.append(index)
    if len(boundaries) == 1:
        return [candidate]
    sections = []
    for start, end in zip(boundaries, boundaries[1:] + [len(rows)]):
        fragment = deepcopy(soup)
        for index, row in reversed(list(enumerate(fragment.find_all("tr")))):
            if not start <= index < end:
                row.decompose()
        sections.append(replace(candidate, html=str(fragment)))
    return sections


def prepare_st_tables(candidates, source_pdf_path=None):
    # 先按完整重复表头划分片段；数据行只迁移，不删除也不改值。
    candidates = [section for candidate in candidates
                  for section in _split_ball_header_sections(candidate)]
    parsed = [_ball_header(candidate) for candidate in candidates]
    references = defaultdict(list)
    for candidate, result in zip(candidates, parsed):
        if result is None:
            continue
        _, rows, first, second, labels, top_count = result
        n = len(labels)
        tail = [_text(cell) for cell in second[n:]]
        if (top_count >= n and tail and re.match(r"pin\s*name", tail[0], re.I)
                and _aligned(rows, n)):
            references[_key(candidate.title)].append((labels, tail))

    repaired = []
    for candidate, result in zip(candidates, parsed):
        if result is not None:
            soup, rows, first, second, labels, top_count = result
            refs = references.get(_key(candidate.title), [])
            refs = [(names, tail) for names, tail in refs
                    if len(labels) <= len(names)
                    and len(names) - len(labels) <= 1
                    and all(_compatible(a, b) for a, b in zip(labels, names))
                    and (_aligned(rows, len(names))
                         or (len(labels) == len(names) and top_count >= len(names)
                             and len(second) > len(names)
                             and re.match(r"pin\s*name", _text(second[len(names)]), re.I)))]
            if refs:
                width = max(len(names) for names, _ in refs)
                refs = [(names, tail) for names, tail in refs if len(names) == width]
                if all(all(_compatible(a, b) for a, b in zip(refs[0][0], names)) for names, _ in refs):
                    names, tail = refs[0]
                    # 同一表中相互吻合的片段共同为表头提供证据。
                    peers = [r[4] for c, r in zip(candidates, parsed)
                             if r is not None and _key(c.title) == _key(candidate.title)
                             and all(_compatible(a, b) for a, b in zip(r[4], names))]
                    names = [Counter([p[i] for p in peers if len(p) > i]).most_common(1)[0][0]
                             for i in range(width)]
                    new_row = soup.new_tag("tr")
                    for value in [*("Pin number " + name for name in names), *tail]:
                        cell = soup.new_tag("th")
                        cell.string = value
                        new_row.append(cell)
                    rows[0].replace_with(new_row)
                    rows[1].decompose()
                    candidate = replace(candidate, html=str(soup))
        else:
            candidate = _repair_simple_header(candidate)
        repaired.append(candidate)
    return repaired


def _repair_simple_header(candidate):
    if not re.search(r"\bpin\s+(?:description|definition)", candidate.title, re.I):
        return candidate
    soup = BeautifulSoup(candidate.html, "html.parser")
    rows = soup.find_all("tr")
    if len(rows) < 2:
        return candidate
    cells = _cells(rows[0])
    labels = [_key(_text(cell)) for cell in cells]
    replacements = {}
    if len(labels) >= 3 and labels[:3] in (["pin", "symbol", "function"], ["pin", "symbol", "functions"]):
        replacements = {0: "Pin number", 1: "Pin name", 2: "Description"}
    elif len(labels) >= 4 and labels[0] == "symbol" and labels[-1] == "description" and all(_package(_text(cell)) for cell in cells[1:-1]):
        replacements = {0: "Pin name", **{i: "Pin number " + _text(cells[i]) for i in range(1, len(cells) - 1)}}
    if not replacements:
        return candidate
    for index, value in replacements.items():
        cells[index].clear()
        cells[index].append(value)
    return replace(candidate, html=str(soup))
