"""ST type 列选择与完整提取路径回归。"""
import unittest
from unittest.mock import patch
import extract.pin_package_extractor as extractor
from extract.pin_package_extractor import ColumnDecision, TableCandidate, TableDecision
from extract.package_catalog_resolver import PackageCatalogEntry, PackageCatalogResolution, PackageAssignment
from extract.vendors.st.type_handlers import select_st_type_columns, resolve_st_type_value
from extract.vendors.st.table_handlers import match_st_table


class STMultiTypeTest(unittest.TestCase):
    def test_first_two_in_source_order_not_model_order(self):
        columns = [ColumnDecision(4, 'Type', 'type'), ColumnDecision(2, 'Type', 'type'),
                   ColumnDecision(0, 'PIN NO', 'pin_no'), ColumnDecision(3, 'Type', 'type')]
        selected = select_st_type_columns(columns)
        self.assertEqual(sorted(c.index for c in selected if c.field_name == 'type'), [2, 3])
        self.assertEqual(resolve_st_type_value(['1', 'EN', 'I', 'FT', 'ignored'], columns), 'I_FT')

    def test_single_empty_equal_and_missing_values(self):
        one = [ColumnDecision(2, 'Type', 'type')]
        two = one + [ColumnDecision(3, 'Type', 'type')]
        self.assertEqual(resolve_st_type_value(['1', 'EN', 'I'], one), 'I')
        self.assertEqual(resolve_st_type_value(['1', 'EN', 'I', 'I'], two), 'I_I')
        self.assertEqual(resolve_st_type_value(['1', 'EN', '', 'FT'], two), 'FT')
        self.assertEqual(resolve_st_type_value(['1', 'EN', 'I'], two), 'I')
        self.assertEqual(resolve_st_type_value(['1', 'EN', '', ''], two), '')
        self.assertIsNone(resolve_st_type_value(['1'], []))

    def test_rules_and_other_vendor_isolation(self):
        from extract.semantic_classifier import build_schema_prompt_payload
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'ST'}):
            columns = extractor.classify_columns(['Pin number', 'Pin name', 'Pin type', 'I/O structure'],
                                                [['1', 'EN', 'I/O', 'FT']])
            self.assertEqual([c.index for c in columns if c.field_name == 'type'], [2, 3])
            self.assertEqual(resolve_st_type_value(['1', 'EN', 'I/O', 'FT'], columns), 'I/O_FT')
            self.assertTrue(any('first two' in r for r in build_schema_prompt_payload('', [], [])['rules']))
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'TI'}):
            self.assertTrue(any('select only the type most directly' in r
                                for r in build_schema_prompt_payload('', [], [])['rules']))
        headers = ['PIN NO.', 'PIN NAME', 'TYPE', 'TYPE', 'TYPE']
        rows = [['1', 'EN', 'I', 'O', 'I/O']]
        for vendor, expected in [('ST', 2), ('TI', 1)]:
            with patch.dict('os.environ', {'EXTRACT_VENDOR': vendor}):
                columns = extractor.classify_columns(headers, rows)
            self.assertEqual(sum(c.field_name == 'type' for c in columns), expected)

    def _extract(self, multi=False, fallback=False):
        headers = (['Pin number QFN32', 'Pin number WLCSP36', 'Pin name', 'Pin type', 'Type', 'Type']
                   if multi else ['PIN NO.', 'PIN NAME', 'TYPE', 'TYPE', 'TYPE'])
        row = ['1', 'A1', 'EN', 'I', 'FT', 'ignored'] if multi else ['1', 'EN', 'I', 'FT', 'ignored']
        no_count = 2 if multi else 1
        name_index = no_count
        type_start = name_index + 1
        columns = [ColumnDecision(i, headers[i], 'pin_no') for i in range(no_count)]
        columns += [ColumnDecision(name_index, headers[name_index], 'pin_name')]
        columns += [ColumnDecision(i, headers[i], 'type') for i in reversed(range(type_start, len(headers)))]
        table = TableCandidate(html='<table><tr>'+''.join('<td>'+h+'</td>' for h in headers)+'</tr>'
                 '<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr></table>',
                 page_idx=0, title='Pin descriptions')
        entries = [PackageCatalogEntry(package_key=str(i), package_type='QFN' if i == 0 else 'WLCSP')
                   for i in range(no_count)]
        assignments = {} if fallback else {(0, i): PackageAssignment(str(i), e.package_type, 'test')
                                          for i, e in enumerate(entries)}
        resolution = PackageCatalogResolution(entries, assignments)
        with patch.dict('os.environ', {'EXTRACT_VENDOR': 'ST'}), \
             patch.object(extractor, 'decide_all_tables', return_value={0: TableDecision(True, columns=columns)}), \
             patch.object(extractor, 'resolve_document_package_catalog', return_value=resolution):
            return extractor.extract_pin_package_info_from_table_candidates([table])

    def test_single_package_type_output(self):
        result = self._extract()
        self.assertEqual(result[0]['group_list'][0]['pin_list'][0]['type'], 'I_FT')

    def test_multi_package_type_output(self):
        result = self._extract(multi=True)
        records = [p for package in result for g in package.get('group_list', []) for p in g['pin_list']]
        self.assertEqual([p['pin_no'] for p in records], ['1', 'A1'])
        self.assertEqual([p['type'] for p in records], ['I_FT', 'I_FT'])

    def test_empty_output_fallback_type_output(self):
        result = self._extract(multi=True, fallback=True)
        records = [p for package in result for g in package.get('group_list', []) for p in g['pin_list']]
        self.assertEqual([p['type'] for p in records], ['I_FT', 'I_FT'])

    def test_st_special_table_selects_two_types(self):
        headers = ['Pin number QFN32', 'Pin name', 'Pin type', 'Type', 'Type']
        match = match_st_table('Pin descriptions', headers, [['1', 'EN', 'I', 'FT', 'ignored']])
        self.assertIsNotNone(match)
        self.assertEqual([c.index for c in match.columns if c.field_name == 'type'][:2], [2, 3])
