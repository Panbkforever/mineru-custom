"""ST 物理引脚表的源表头修复，不改写数据行。"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from difflib import SequenceMatcher
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


def _table_key(title):
    return _key(title).replace('continued', '')


def _generic_pin_header(candidate):
    soup = BeautifulSoup(candidate.html, 'html.parser')
    rows = soup.find_all('tr')
    if len(rows) < 4 or any(cell.has_attr('rowspan') or cell.has_attr('colspan')
                            for row in rows[:3] for cell in _cells(row)):
        return None
    top = [_text(cell) for cell in _cells(rows[0])]
    n = 0
    for value in top:
        if not _key(value).startswith(('pinnumber', 'pinno')):
            break
        n += 1
    if n < 2 or n >= len(top) or not _key(top[n]).startswith('pinname'):
        return None
    for level in (1, 2):
        if len(rows) <= level + 1:
            continue
        labels = [_text(cell) for cell in _cells(rows[level])]
        if sum(_package(value) for value in labels[:max(n, 2)]) < 2:
            continue
        end = next((i for i, value in enumerate(labels)
                    if _key(value).startswith(('pinname', 'pintype'))), len(labels))
        return soup, rows, n, level, labels[:end]
    return None


def _split_ball_pin_column(candidate):
    context = getattr(candidate, 'group_context', '') or ''
    packages = {'ball': set(), 'pin': set()}
    for line in context.splitlines():
        role = re.search(r'\b(ball|pin)\s+assignment\b', line, re.I)
        labels = re.findall(r'\b(?:WLCSP|[A-Z]*BGA|[A-Z]*QFN|[A-Z]*QFP)\s*-?\s*\d+\b', line, re.I)
        if role and len(labels) == 1:
            packages[role.group(1).lower()].add(re.sub(r'\s+', '', labels[0]))
    if any(len(packages[role]) != 1 for role in ('ball', 'pin')):
        return candidate
    soup = BeautifulSoup(candidate.html, 'html.parser')
    rows = soup.find_all('tr')
    if len(rows) < 2:
        return candidate
    headers = _cells(rows[0])
    columns = [i for i, c in enumerate(headers)
               if re.fullmatch(r'(?:i\s*/\s*o\s*)?balls?\s*/\s*pins?', _text(c), re.I)]
    if len(columns) != 1:
        return candidate
    col = columns[0]
    values = []
    for row in rows[1:]:
        cells = _cells(row)
        if len(cells) != len(headers) or any(c.has_attr('rowspan') or c.has_attr('colspan') for c in cells):
            return candidate
        parts = _text(cells[col]).split('/')
        if len(parts) != 2:
            return candidate
        values.append([part.strip() for part in parts])
    if not any(re.fullmatch(r'[A-Za-z]+\d+', parts[0]) and re.fullmatch(r'\d+', parts[1]) for parts in values):
        return candidate
    if any((a not in {'', '-', 'NA', 'N/A'} and not re.fullmatch(r'[A-Za-z]+\d+', a)) or
           (b not in {'', '-', 'NA', 'N/A'} and not re.fullmatch(r'\d+', b)) for a, b in values):
        return candidate
    # 仅在章节和每一行的列结构均能对应时展开，原始数据行全部保留。
    headers[col].clear()
    headers[col].append('Pin number ' + next(iter(packages['ball'])))
    other = soup.new_tag('th')
    other.string = 'Pin number ' + next(iter(packages['pin']))
    headers[col].insert_after(other)
    for row, (ball, pin) in zip(rows[1:], values):
        cell = _cells(row)[col]
        cell.clear()
        cell.append(ball)
        other = soup.new_tag('td')
        other.string = pin
        cell.insert_after(other)
    return replace(candidate, html=str(soup))


def _data_aligned(rows, start, n):
    data = rows[start:start + 12]
    hits = 0
    for row in data:
        cells = _cells(row)
        if len(cells) > n + 1 and _text(cells[n]) and re.fullmatch(
                r'I/O|I|O|S|A|ANA|P|Power|Supply', _text(cells[n + 1]), re.I):
            hits += 1
    return hits >= min(2, len(data)) and bool(data)


def _sequence_similarity(first, second):
    return SequenceMatcher(None, [_key(x) for x in first],
                           [_key(x) for x in second], autojunk=False).ratio()


def _repair_generic_pin_headers(candidates):
    parsed = [_generic_pin_header(candidate) for candidate in candidates]
    references = defaultdict(list)
    for index, (candidate, item) in enumerate(zip(candidates, parsed)):
        if item is None:
            continue
        _, rows, n, level, labels = item
        if (len(labels) == n and sum(_package(x) for x in labels) >= max(2, n * .7)
                and _data_aligned(rows, level + 1, n)):
            references[(_table_key(candidate.title), level, n)].append((index, item))
    result = list(candidates)
    for index, (candidate, item) in enumerate(zip(candidates, parsed)):
        if item is None:
            continue
        soup, rows, original_n, level, labels = item
        choices = []
        for (title, reference_level, n), entries in references.items():
            if title != _table_key(candidate.title) or reference_level != level:
                continue
            before = [entry for entry in entries if entry[0] < index]
            after = [entry for entry in entries if entry[0] > index]
            if not before or not after or not _data_aligned(rows, level + 1, n):
                continue
            for _, left in reversed(before[-3:]):
                for _, right in after[:3]:
                    if level == 2:
                        current_models = [_text(c) for c in _cells(rows[1])[:original_n]]
                        left_models = [_text(c) for c in _cells(left[1][1])[:n]]
                        right_models = [_text(c) for c in _cells(right[1][1])[:n]]
                        if (_sequence_similarity(left_models, right_models) < .9
                                or _sequence_similarity(current_models, left_models) < .75
                                or _sequence_similarity(current_models, right_models) < .75):
                            continue
                    if (_sequence_similarity(left[4], right[4]) >= .85
                            and _sequence_similarity(labels, left[4]) >= .75
                            and _sequence_similarity(labels, right[4]) >= .75):
                        choices.append((n, left, right))
                        break
                if choices and choices[-1][0] == n:
                    break
        if len(choices) != 1:
            continue
        n, left, right = choices[0]
        if original_n == n and len(labels) == n and _data_aligned(rows, level + 1, original_n):
            continue
        # 仅修复列结构有明确异常的续表，保留全部数据行与单元格。
        ref_rows = left[1]
        ref_labels = left[4]
        model = [_text(c) for c in _cells(ref_rows[1])] if level == 2 else []
        new_row = soup.new_tag('tr')
        for i, label in enumerate(ref_labels):
            cell = soup.new_tag('th')
            cell.string = 'Pin number ' + ((model[i] + ' ') if level == 2 else '') + label
            new_row.append(cell)
        for value in ['Pin name', 'Pin type']:
            cell = soup.new_tag('th')
            cell.string = value
            new_row.append(cell)
        for cell in _cells(ref_rows[level])[n + 2:]:
            new_row.append(deepcopy(cell))
        rows[0].replace_with(new_row)
        for row in rows[1:level + 1]:
            row.decompose()
        result[index] = replace(candidate, html=str(soup))
    return result


def _preserve_shared_package_columns(candidate):
    # 复合表头可能使同一封装名跨多个物理列出现，不能按名称合并列。
    from extract.vendors.st.table_handlers import _split_combined_st_package_labels

    item = _generic_pin_header(candidate)
    if item is None:
        return candidate
    soup, rows, n, level, labels = item
    if level != 1 or len(labels) != n or n > 26 or not _data_aligned(rows, 2, n):
        return candidate
    expanded = [_split_combined_st_package_labels(re.sub(r'-(?=[A-Za-z])', ' - ', value)) or [value]
                for value in labels]
    flattened = [_key(value) for group in expanded for value in group]
    if not any(len(group) > 1 for group in expanded) or len(flattened) == len(set(flattened)):
        return candidate
    header = soup.new_tag('tr')
    for index, original in enumerate(labels):
        cell = soup.new_tag('th')
        cell['data-st-source-header'] = original
        cell.string = 'Pin number Package ' + chr(ord('A') + index)
        header.append(cell)
    for value in _cells(rows[0])[n:]:
        header.append(deepcopy(value))
    rows[0].replace_with(header)
    rows[1].decompose()
    return replace(candidate, html=str(soup))


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
    candidates = [_split_ball_pin_column(candidate) for candidate in candidates]
    candidates = _repair_generic_pin_headers(candidates)
    candidates = [_preserve_shared_package_columns(candidate) for candidate in candidates]
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
