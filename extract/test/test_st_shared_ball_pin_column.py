from dataclasses import replace

from bs4 import BeautifulSoup

from extract.pin_package_extractor import TableCandidate
from extract.vendors.st.source_tables import _split_ball_pin_column


def _candidate(rows, context):
    html = '<table>' + ''.join('<tr>' + ''.join('<td>' + c + '</td>' for c in r) + '</tr>' for r in rows) + '</table>'
    return TableCandidate(html=html, page_idx=0, title='Pin description', group_context=context)


def test_shared_column_preserves_all_rows_and_requires_evidence():
    context = '2.1 WLCSP20 package - ball assignment\n2.2 QFN20 package - pin assignment'
    original = _candidate([['I/O balls/pins', 'Name', 'Description'], ['D4 / 15', 'VIN', 'Power'],
                           ['- / 01', 'NC', 'Unused'], [' / 02', 'TEST', 'Test']], context)
    repaired = _split_ball_pin_column(original)
    rows = [[c.get_text(' ', strip=True) for c in r.find_all(['td', 'th'])]
            for r in BeautifulSoup(repaired.html, 'html.parser').find_all('tr')]
    assert rows == [['Pin number WLCSP20', 'Pin number QFN20', 'Name', 'Description'],
                    ['D4', '15', 'VIN', 'Power'], ['-', '01', 'NC', 'Unused'], ['', '02', 'TEST', 'Test']]
    assert _split_ball_pin_column(replace(original, group_context='')).html == original.html
    ambiguous = _candidate([['I/O balls/pins', 'Name'], ['D4 / 15 / 3', 'VIN']], context)
    assert _split_ball_pin_column(ambiguous).html == ambiguous.html
