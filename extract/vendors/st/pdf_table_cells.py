"""用原 PDF 的同一物理行证据恢复 ST 多封装表中的缺失单元格。"""

from collections import defaultdict
from pathlib import Path
import re

from bs4 import BeautifulSoup

from extract.vendors.st.source_tables import _cells, _key, _package, _text
from post_table.restore_cell_line_breaks import _iter_html_spans, _physical_table_sources


def _package_count(rows):
    if len(rows) < 3:
        return 0
    first, second = _cells(rows[0]), _cells(rows[1])
    if sum(_key(_text(cell)).startswith('pinballname') for cell in first) < 2:
        return 0
    count = 0
    for cell in second:
        if not _package(_text(cell)):
            break
        count += 1
    return count if count >= 2 else 0


def _row_signature(row):
    return tuple(_text(cell) for cell in _cells(row))


def _missing_value(text_page, prefix, name, kind):
    # 必须匹配完整有序前缀，而不是仅凭名称或某个编号认领原始行。
    terms = [*prefix, None, name, kind]
    parts = [r'(?P<v%d>%s)' % (i, re.escape(value) if value is not None else r'[A-Za-z]*\d+[A-Za-z0-9]*|-')
             for i, value in enumerate(terms)]
    pattern = re.compile(r'(?m)^\s*' + r'[ \t]+'.join(parts) + r'(?=[ \t\r\n]|$)')
    text = text_page.get_text_range(0, text_page.count_chars())
    matches = []
    for match in pattern.finditer(text):
        positions = []
        for index in range(len(terms)):
            start, end = match.span('v%d' % index)
            try:
                boxes = [text_page.get_charbox(i) for i in range(start, end)]
            except Exception:
                break
            positions.append((sum((b[0] + b[2]) / 2 for b in boxes) / len(boxes),
                              sum((b[1] + b[3]) / 2 for b in boxes) / len(boxes)))
        if (len(positions) == len(terms)
                and all(a[0] < b[0] for a, b in zip(positions, positions[1:]))
                and max(y for _, y in positions) - min(y for _, y in positions) < 4):
            matches.append(match.group('v%d' % len(prefix)))
    return matches[0] if len(matches) == 1 else None


def restore_st_pin_cells(middle_json, pdf_path):
    """仅恢复逐页源表和 PDF 同行文字唯一吻合的遗漏单元格。"""
    if not pdf_path or not Path(pdf_path).is_file():
        return 0
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return 0
    evidence = defaultdict(list)
    occurrences = defaultdict(int)
    try:
        for source in _physical_table_sources(middle_json.get('pdf_info', [])):
            soup = BeautifulSoup(source.html, 'html.parser')
            rows = soup.find_all('tr')
            count = _package_count(rows)
            if not count or not 0 <= source.page_index < len(doc):
                continue
            page = doc[source.page_index]
            text_page = page.get_textpage()
            try:
                for row in rows[2:]:
                    cells = _cells(row)
                    if len(cells) <= count + 1 or _text(cells[-1]):
                        continue
                    if any(cell.has_attr('rowspan') or cell.has_attr('colspan') for cell in cells):
                        continue
                    values = [_text(cell) for cell in cells]
                    name, kind = values[count - 1:count + 1]
                    prefix = values[:count - 1]
                    if (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
                            or not re.fullmatch(r'I/O|I|O|S|A|ANA', kind, re.I)
                            or sum(bool(re.fullmatch(r'[A-Za-z]*\d+[A-Za-z0-9]*', x)) for x in prefix) < 3
                            or any(not x or re.search(r'\s', x) for x in prefix)):
                        continue
                    occurrences[(count, _row_signature(row))] += 1
                    value = _missing_value(text_page, prefix, name, kind)
                    if value is not None:
                        evidence[(count, _row_signature(row))].append(value)
            finally:
                text_page.close()
    finally:
        doc.close()
    # 同一源行存在多个匹配时不决定归属，避免跨页误认。
    evidence = {key: values[0] for key, values in evidence.items()
                if len(values) == 1 and occurrences[key] == 1}
    changed = 0
    for page in middle_json.get('pdf_info', []):
        for span in _iter_html_spans(page):
            html = span.get('html', '')
            if 'pin/ball name' not in html.lower():
                continue
            soup = BeautifulSoup(html, 'html.parser')
            rows = soup.find_all('tr')
            count = _package_count(rows)
            if not count:
                continue
            touched = False
            for row in rows[2:]:
                cells = _cells(row)
                value = evidence.get((count, _row_signature(row)))
                if value is None or len(cells) <= count + 1 or _text(cells[-1]):
                    continue
                if any(cell.has_attr('rowspan') or cell.has_attr('colspan') for cell in cells):
                    continue
                new_cell = soup.new_tag(cells[count - 1].name)
                new_cell.string = value
                cells[count - 1].insert_before(new_cell)
                cells[-1].decompose()
                touched = True
                changed += 1
            if touched:
                span['html'] = str(soup)
    return changed
