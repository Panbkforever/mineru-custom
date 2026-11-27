"""ST 物理编号表头别名回归，不调用模型、不筛数据行。"""

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import extract.pin_package_extractor as extractor
from extract.vendors.st.source_tables import prepare_st_tables
from extract.vendors.st.table_handlers import (
    _package_label_from_pin_header,
    match_st_table,
)


def candidate(html, title="Table 1. Pin description"):
    return extractor.TableCandidate(html=html, title=title, page_idx=0)


def pins(result):
    return [pin for pkg in result for group in pkg["group_list"] for pin in group["pin_list"]]


def body(html):
    return [str(row) for row in BeautifulSoup(html, "html.parser").find_all("tr")[1:]]


class STPhysicalHeaderAliasesTest(unittest.TestCase):
    def extract(self, table):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "ST"}), \
             patch("extract.semantic_classifier.classify_table_schema_batch", side_effect=AssertionError("禁止调用模型")), \
             patch("extract.semantic_classifier.classify_package_catalog_tables", side_effect=AssertionError("禁止调用模型")):
            return extractor.extract_pin_package_info_from_table_candidates([table])

    def test_plural_package_pin_headers_and_two_types(self):
        table = candidate("""<table>
        <tr><th colspan="2">Pins</th><th rowspan="2">Pin name</th>
            <th rowspan="2">Pin Type(1)</th><th rowspan="2">I/O structure</th>
            <th rowspan="2">Alternate functions</th></tr>
        <tr><th>LQFP64</th><th>UFBGA100</th></tr>
        <tr><td>1</td><td>A1</td><td>PA0</td><td>I/O</td><td>FT</td><td>AF0</td></tr>
        <tr><td>-</td><td>B1</td><td>NC</td><td>S</td><td>-</td><td>-</td></tr>
        </table>""", "Table 7. MCU pin definitions")
        result = self.extract(table)
        self.assertEqual(len(result), 2)
        self.assertEqual([p["pin_no"] for p in pins(result)], ["1", "-", "A1", "B1"])
        self.assertEqual([p["pin_name"] for p in pins(result)], ["PA0", "NC", "PA0", "NC"])
        self.assertEqual([p["type"] for p in pins(result)], ["I/O_FT", "S_-", "I/O_FT", "S_-"])
        self.assertEqual(_package_label_from_pin_header("Pins LQFP64"), "LQFP64")

    def test_definition_match_does_not_filter_any_row(self):
        headers = ["Pins LQFP64", "Pins WLCSP104", "Pin name", "Pin type", "I/O structure"]
        rows = [["1", "A1", "EN", "I", "FT"], ["", "-", "NC", "", ""],
                ["NA", "N/A", "Reserved", "S", "-"]]
        match = match_st_table("MCU pin definitions", headers, rows)
        self.assertIsNotNone(match)
        self.assertEqual([c.index for c in match.columns if c.field_name == "pin_no"], [0, 1])
        self.assertEqual(match.included_row_indexes, frozenset(range(len(rows))))

    def test_bump_group_header_repair_keeps_body(self):
        table = candidate("""<table><tr><th colspan="2">Bump</th><th>Bump name</th>
        <th colspan="2">Description</th></tr>
        <tr><td rowspan="2">Power</td><td>IN</td><td>E5-F5</td><td colspan="2">Supply</td></tr>
        <tr><td>BAT</td><td>A5-B5</td><td colspan="2">Battery</td></tr>
        </table>""")
        repaired = prepare_st_tables([table])[0]
        self.assertEqual(body(table.html), body(repaired.html))
        headers = extractor.parse_spanned_table(repaired.html)[0]
        self.assertEqual(headers, ["Group", "Pin name", "Pin number", "Description", "Description"])
        result = self.extract(table)
        self.assertEqual({p["pin_name"] for p in pins(result)}, {"IN", "BAT"})
        self.assertTrue(pins(result))
        self.assertEqual(prepare_st_tables([repaired])[0].html, repaired.html)

    def test_number_sign_and_symbol_headers_keep_packages_and_body(self):
        for degree in ("°", "º"):
            with self.subTest(degree=degree):
                table = candidate(f"""<table><tr><th>Pin n{degree}DFN10 (3 x 3 mm)</th>
                <th>Pin n{degree}Flip Chip 9</th><th>Symbol</th><th>Note</th></tr>
                <tr><td>1,2,3,4,5</td><td>C1,C2,C3</td><td>VOUT/source</td><td>Output</td></tr>
                <tr><td>7</td><td>A1</td><td>I-lim +</td><td>Limit</td></tr>
                <tr><td>Exposed pad</td><td>B1,B2,B3</td><td>VCC</td><td>Supply</td></tr>
                </table>""")
                repaired = prepare_st_tables([table])[0]
                self.assertEqual(body(table.html), body(repaired.html))
                self.assertEqual(prepare_st_tables([repaired])[0].html, repaired.html)
                result = self.extract(table)
                self.assertEqual(len(result), 2)
                records = [p for p in pins(result) if p["pin_name"] == "VOUT/source"]
                self.assertEqual(len(records), 8)
                self.assertTrue(any(p["pin_no"] == "EP" and p["pin_name"] == "VCC" for p in pins(result)))

    def test_flattened_bump_header_keeps_body(self):
        table = candidate("""<table><tr><td>Bump</td><td>Bump</td><td>Bump name</td>
        <td>Description</td><td>Description</td></tr>
        <tr><td>Power</td><td>IN</td><td>E5-F5</td><td>Supply</td><td>Supply</td></tr>
        <tr><td>Digital</td><td>TEST</td><td>-</td><td>Test</td><td>Test</td></tr>
        </table>""")
        repaired = prepare_st_tables([table])[0]
        self.assertEqual(body(repaired.html), body(table.html))
        records = pins(self.extract(table))
        self.assertEqual({p["pin_name"] for p in records}, {"IN", "TEST"})
        self.assertTrue(any(p["pin_name"] == "TEST" and p["pin_no"] == "-" for p in records))

    def test_unrelated_function_table_is_not_promoted(self):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "ST"}):
            self.assertEqual(extractor.classify_header("Pins source"), ("", 0))
        headers = ["Port", "Pin name", "Alternate functions"]
        self.assertIsNone(match_st_table("Port PA alternate functions", headers, [["PA0", "EN", "AF0"]]))
        self.assertTrue(extractor.is_non_physical_port_function_table("Port PA alternate functions", headers))
        table = candidate("<table><tr><th>Bump</th><th>Bump name</th><th>Description</th></tr>"
                          "<tr><td>IN</td><td>E5</td><td>Supply</td></tr></table>")
        self.assertEqual(prepare_st_tables([table])[0].html, table.html)

    def test_ti_does_not_gain_st_header_alias(self):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "TI"}):
            self.assertEqual(extractor.classify_header("Pins LQFP64"), ("", 0))


if __name__ == "__main__":
    unittest.main()
