"""在 TI 表格区域识别前移除确认重复的续页表头。"""

from dataclasses import replace
import re

from bs4 import BeautifulSoup


def _row_key(row):
    cells = row.find_all(['td', 'th'], recursive=False)
    if any(cell.has_attr('rowspan') or cell.has_attr('colspan') for cell in cells):
        return None
    return tuple(re.sub(r'[^a-z0-9]', '', cell.get_text(' ', strip=True).lower()) for cell in cells)


def strip_ti_repeated_two_row_headers(table):
    """要求连续两行逐列匹配原始两层表头，数据行不参与筛选。"""
    soup = BeautifulSoup(table.html, 'html.parser')
    rows = soup.find_all('tr')
    if len(rows) < 6:
        return table
    # 跨行属性可能依赖被删表头的网格位置，无法安全证明时不修复。
    if any(cell.has_attr('rowspan') for cell in soup.find_all(['td', 'th'])):
        return table
    first, second = _row_key(rows[0]), _row_key(rows[1])
    if not first or not second or len(first) < 4 or len(first) != len(second) or first == second:
        return table
    joined = ' '.join(first)
    if not ('pinno' in joined or 'pinnumber' in joined or 'ballnumber' in joined):
        return table
    if not ('name' in joined or 'signal' in joined):
        return table
    repeats = []
    index = 2
    while index + 1 < len(rows):
        if _row_key(rows[index]) == first and _row_key(rows[index + 1]) == second:
            repeats.append((rows[index], rows[index + 1]))
            index += 2
        else:
            index += 1
    if not repeats:
        return table
    for upper, lower in repeats:
        upper.decompose()
        lower.decompose()
    return replace(table, html=str(soup))


def prepare_ti_tables(tables, source_pdf_path=None):
    """在确定数据区域前修复被拼接进表内的重复多层表头。"""
    return [strip_ti_repeated_two_row_headers(table) for table in tables]
