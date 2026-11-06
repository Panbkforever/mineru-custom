import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from extract.pin_package_extractor import TableCandidate, extract_pin_package_info_from_table_candidates
from extract.vendors.st.source_tables import _compatible, prepare_st_tables, _split_ball_header_sections, _repair_generic_pin_headers
from extract.vendors.registry import get_vendor_profile


def table(title, rows):
    html = '<table>' + ''.join('<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows) + '</table>'
    return TableCandidate(html=html, page_idx=0, title=title)


def data_rows(candidate, start):
    soup = BeautifulSoup(candidate.html, 'html.parser')
    return [[cell.get_text(' ', strip=True) for cell in row.find_all(['td', 'th'])]
            for row in soup.find_all('tr')[start:]]


class StSourceHeaderTest(unittest.TestCase):
    def test_single_package_rows_are_preserved_and_extracted(self):
        rows = [['Pin #', 'Symbol', 'Functions'], ['1', 'VDD', 'Supply'],
                ['-', 'NC', 'Unused'], ['', 'TEST', 'Test']]
        original = table('Table 1. Pin description', rows)
        repaired = prepare_st_tables([original])[0]
        self.assertEqual(data_rows(original, 1), data_rows(repaired, 1))
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'ST'}):
            result = extract_pin_package_info_from_table_candidates([original], use_semantic_classifier=False)
        pins = [pin for package in result for group in package['group_list'] for pin in group['pin_list']]
        self.assertTrue(any(pin['pin_name'] == 'VDD' for pin in pins))
        self.assertTrue(any(pin['pin_name'] == 'NC' for pin in pins))
        self.assertTrue(any(pin['pin_name'] == 'TEST' for pin in pins))

    def test_two_package_headers_and_rows(self):
        original = table('Table 1. Pin description', [
            ['Symbol', 'SOT23-5L', 'Flip-Chip 4', 'Description'],
            ['VDD', '1', 'A1', 'Supply'], ['NC', '-', '', 'Unused']])
        repaired = prepare_st_tables([original])[0]
        self.assertEqual(data_rows(original, 1), data_rows(repaired, 1))
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'ST'}):
            result = extract_pin_package_info_from_table_candidates([original], use_semantic_classifier=False)
        self.assertEqual({x['pkg'] for x in result}, {'SOT23-5L', 'Flip-Chip 4'})

    def test_ball_header_cross_page_and_ambiguous_evidence(self):
        title = 'Table 7. Pin/ball definition'
        first = table(title, [['Pin/ball name', 'Pin/ball name', 'Pin name', 'Pin type'],
                              ['LQFP32', 'TFBGA36', 'Pin name', 'Pin type'],
                              ['1', 'A1', 'PA0', 'I/O'], ['-', 'A2', 'NC', 'S']])
        second = table(title, [['Pin/ball name', 'Pin/ball name', 'Pin name', 'Pin type'],
                               ['LQFP32', 'TFBGA36', 'Pin type', ''],
                               ['2', 'A3', 'PA1', 'I/O']])
        repaired = prepare_st_tables([first, second])
        for before, after in zip((first, second), repaired):
            self.assertEqual(data_rows(before, 2), data_rows(after, 1))
            self.assertNotEqual(before.html, after.html)
        unrelated = table(title, [['Pin/ball name', 'Pin/ball name', 'Pin name', 'Pin type'],
                                  ['LQFP48', 'TFBGA36', 'Pin type', ''],
                                  ['2', 'A3', 'PA1', 'I/O']])
        self.assertEqual(prepare_st_tables([first, unrelated])[1].html, unrelated.html)
        self.assertFalse(_compatible('LQFP32', 'LQFP36'))
        self.assertFalse(_compatible('LQFP32', 'TQFP32'))

    def test_other_vendor_isolation(self):
        self.assertIsNone(get_vendor_profile('TI').source_table_repairer)
        self.assertIsNot(get_vendor_profile('ST').source_table_repairer,
                         get_vendor_profile('ELAN').source_table_repairer)

    def test_embedded_repeated_header_keeps_every_data_row(self):
        title = 'Table 7. Pin/ball definition'
        head1 = ['Pin/ball name(1)', 'Pin/ball name(1)', 'Pin name', 'Pin type']
        head2 = ['LQFP32', 'TFBGA36', 'Pin name', 'Pin type']
        data = [['1', 'A1', 'PA0', 'I/O'], ['-', '', 'NC', 'S'],
                ['', 'A2', 'Reserved', ''], ['Pin/ball name', '3', 'TEST', 'I/O']]
        original = table(title, [head1, head2, *data[:2], head1, head2, *data[2:]])
        sections = _split_ball_header_sections(original)
        self.assertEqual(len(sections), 2)
        self.assertEqual([row for section in sections for row in data_rows(section, 2)], data)
        repaired = prepare_st_tables([original])
        self.assertEqual([row for section in repaired for row in data_rows(section, 1)], data)

    def test_latex_header_uses_same_table_reference(self):
        title = 'Table 7. Pin/ball definition'
        complete = table(title, [['Pin/ball name', 'Pin/ball name', 'Pin name', 'Pin type'],
                                 ['LQFP32', 'TFBGA36', 'Pin name', 'Pin type'],
                                 ['1', 'A1', 'PA0', 'I/O'], ['-', 'A2', 'NC', 'S']])
        continued = table(title, [['$Pin/ball name^{(1)}$ (2)', '$Pin/ball name^{(1)}$ (2)', 'Pin name', 'Pin type'],
                                  ['LQFP32', 'TFBGA36', 'Pin type', ''],
                                  ['2', 'A3', 'PA1', 'I/O']])
        repaired = prepare_st_tables([complete, continued])
        self.assertNotEqual(repaired[1].html, continued.html)
        self.assertEqual(data_rows(continued, 2), data_rows(repaired[1], 1))
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'ST'}):
            result = extract_pin_package_info_from_table_candidates([complete, continued], use_semantic_classifier=False)
        self.assertTrue(any(pin.get('pin_name') == 'PA1' for package in result
                            for group in package['group_list'] for pin in group['pin_list']))

    def test_recover_missing_multilayer_header_without_changing_data(self):
        title = 'Table 7. Pin definitions (continued)'
        labels = ['LQFP32', 'LQFP48', 'LQFP64', 'LQFP100', 'TFBGA132', 'TFBGA169']
        top = ['Pin number'] * 6 + ['Pin name', 'Pin type']
        data = [['1', '-', '', 'NC', 'A1', 'B1', 'PA0', 'I/O'],
                ['2', '3', '4', '5', 'A2', 'B2', 'PA1', 'I/O']]
        complete = table(title, [top, labels + ['Pin name', 'Pin type'], *data])
        damaged = table(title, [top[:-3] + top[-2:] + [''], labels[:-1] + ['Pin name', 'Pin type', ''], *data])
        result = _repair_generic_pin_headers([complete, damaged, complete])
        self.assertNotEqual(result[1].html, damaged.html)
        self.assertEqual(data_rows(result[1], 1), data)
        self.assertEqual(data_rows(complete, 2), data)

    def test_different_table_never_supplies_missing_header(self):
        labels = ['LQFP32', 'LQFP48', 'LQFP64', 'LQFP100', 'TFBGA132', 'TFBGA169']
        data = [['1', '-', '', 'NC', 'A1', 'B1', 'PA0', 'I/O'],
                ['2', '3', '4', '5', 'A2', 'B2', 'PA1', 'I/O']]
        top = ['Pin number'] * 6 + ['Pin name', 'Pin type']
        reference = table('Table 1. Pin definition', [top, labels + ['Pin name', 'Pin type'], *data])
        damaged = table('Table 2. Pin definition', [top[:-3] + top[-2:] + [''], labels[:-1] + ['Pin name', 'Pin type', ''], *data])
        self.assertEqual(_repair_generic_pin_headers([reference, damaged, reference])[1].html, damaged.html)

    def test_shared_physical_columns_are_kept_distinct(self):
        headers = ['LQFP32 / UFQFPN32-GP', 'LQFP32 / UFQFPN32-N', 'LQFP48']
        rows = [['Pin number'] * 3 + ['Pin name', 'Pin type'], headers + ['Pin name', 'Pin type'],
                ['17', '-', '21', 'PB2', 'I/O'], ['-', '17', '27', 'PB15', 'I/O']]
        original = table('Table 1. Pin assignment', rows)
        repaired = prepare_st_tables([original])[0]
        self.assertEqual(data_rows(repaired, 1), rows[2:])
        self.assertIn('data-st-source-header', repaired.html)
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'ST'}):
            result = extract_pin_package_info_from_table_candidates([original], use_semantic_classifier=False)
        self.assertEqual(len(result), 3)
        records = [[p for g in x['group_list'] for p in g['pin_list']] for x in result]
        self.assertIn({'pin_no': '17', 'pin_name': 'PB2', 'type': 'I/O'}, records[0])
        self.assertIn({'pin_no': '-', 'pin_name': 'PB2', 'type': 'I/O'}, records[1])
        self.assertIn({'pin_no': '17', 'pin_name': 'PB15', 'type': 'I/O'}, records[1])
