from types import SimpleNamespace

from reportlab.pdfgen import canvas
from bs4 import BeautifulSoup

from extract.vendors.st import pdf_table_cells as repair


def _table():
    rows = [
        ['Pin/ball name'] * 4 + ['Pin name', 'Pin type', 'I/O structure', ''],
        ['LQFP32', 'TFBGA36', 'LQFP48', 'TFBGA64', 'Pin type', 'I/O structure', '', ''],
        ['1', 'A1', '2', 'PA0', 'I/O', 'FT', 'Signal', ''],
        ['-', '', 'NC', '-', 'S', '-', '-', ''],
    ]
    return '<table>' + ''.join('<tr>' + ''.join('<td>' + x + '</td>' for x in row) + '</tr>' for row in rows) + '</table>'


def _pdf(path, text, second_line=None):
    painter = canvas.Canvas(str(path), pagesize=(500, 500))
    painter.drawString(20, 400, text)
    if second_line:
        painter.drawString(20, 380, second_line)
    painter.save()


def _run(monkeypatch, tmp_path, text, second_line=None, duplicate_source=False):
    path = tmp_path / 'source.pdf'
    _pdf(path, text, second_line)
    html = _table()
    span = {'html': html}
    source = SimpleNamespace(html=html, page_index=0)
    monkeypatch.setattr(repair, '_physical_table_sources', lambda _: [source, source] if duplicate_source else [source])
    monkeypatch.setattr(repair, '_iter_html_spans', lambda _: [span])
    changed = repair.restore_st_pin_cells({'pdf_info': [{}]}, path)
    rows = [[x.get_text() for x in tr.find_all('td')] for tr in BeautifulSoup(span['html'], 'html.parser').find_all('tr')]
    return changed, rows


def test_recovers_only_pdf_proven_missing_cell(monkeypatch, tmp_path):
    changed, rows = _run(monkeypatch, tmp_path, '1 A1 2 B2 PA0 I/O FT')
    assert changed == 1
    assert rows[2] == ['1', 'A1', '2', 'B2', 'PA0', 'I/O', 'FT', 'Signal']
    assert rows[3] == ['-', '', 'NC', '-', 'S', '-', '-', '']


def test_no_repair_without_exact_full_row_evidence(monkeypatch, tmp_path):
    changed, rows = _run(monkeypatch, tmp_path, '1 A1 3 B2 PA0 I/O FT')
    assert changed == 0
    assert rows[2] == ['1', 'A1', '2', 'PA0', 'I/O', 'FT', 'Signal', '']


def test_ambiguous_pdf_or_source_never_guesses(monkeypatch, tmp_path):
    changed, _ = _run(monkeypatch, tmp_path, '1 A1 2 B2 PA0 I/O FT', '1 A1 2 B3 PA0 I/O FT')
    assert changed == 0
    changed, _ = _run(monkeypatch, tmp_path, '1 A1 2 B2 PA0 I/O FT', duplicate_source=True)
    assert changed == 0
