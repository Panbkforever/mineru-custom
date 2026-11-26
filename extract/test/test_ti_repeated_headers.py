"""TI 续表两层表头修复。"""
from extract.pin_package_extractor import TableCandidate
from extract.table_header_structure import parse_spanned_table
from extract.vendors.ti.source_tables import strip_ti_repeated_two_row_headers


def _table(html):
    return TableCandidate(html=html, page_idx=0, title='Signal Descriptions')


def test_repeated_two_row_header_is_removed_without_changing_data():
    first = '<tr><td>NAME</td><td>PIN NO.</td><td>PIN NO.</td><td>DESCRIPTION</td></tr>'
    second = '<tr><td>NAME</td><td>PGF/P TP PIN #</td><td>GHH BALL #</td><td>DESCRIPTION</td></tr>'
    repeated = '<tr><td>NAME</td><td>PGF/PTPPIN #</td><td>GHHBALL #</td><td>DESCRIPTION</td></tr>'
    one = '<tr><td>A</td><td>1</td><td>A1</td><td>first</td></tr>'
    two = '<tr><td>B</td><td>2</td><td>B2</td><td>second</td></tr>'
    table = _table('<table>'+first+second+one+first+repeated+two+'</table>')
    before = parse_spanned_table(table.html)
    result = strip_ti_repeated_two_row_headers(table)
    after = parse_spanned_table(result.html)
    assert after == before[:3] + before[5:]
    assert len(after) == 4


def test_partial_or_spanned_headers_are_not_removed():
    html = ('<table><tr><td>NAME</td><td>PIN NO</td><td>PIN NO</td><td>DESC</td></tr>'
            '<tr><td>NAME</td><td>AAA</td><td>BBB</td><td>DESC</td></tr>'
            '<tr><td>A</td><td>1</td><td>A1</td><td>x</td></tr>'
            '<tr><td>NAME</td><td>PIN NO</td><td>PIN NO</td><td>DESC</td></tr>'
            '<tr><td>other</td><td>2</td><td>B2</td><td>x</td></tr>'
            '<tr><td>B</td><td>3</td><td>C3</td><td>x</td></tr></table>')
    table = _table(html)
    assert strip_ti_repeated_two_row_headers(table) is table
