"""验证 Giga 厂家的封装范围、字段歧义和占位行，不运行模型或批量提取。"""

from html import escape
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from extract.pin_package_extractor import TableCandidate, extract_pin_package_info_from_table_candidates
from extract.vendors import get_vendor_profile
from extract.vendors.giga.package_handlers import resolve_giga_scopes, title_package
from extract.vendors.giga.table_handlers import match_giga_table, repair_giga_table


def table(title, headers, rows, page=0):
    html = "<table>" + "".join("<tr>" + "".join("<td>" + escape(value).replace("\n", "<br>") + "</td>"
                                                     for value in row) + "</tr>" for row in [headers] + rows) + "</table>"
    return TableCandidate(html=html, page_idx=page, title=title)


def pins(package):
    return [pin for group in package["group_list"] for pin in group["pin_list"]]


class GigaVendorTableTest(unittest.TestCase):
    headers = ["Pin No.", "Pin Name", "I/O", "Description"]

    def extract(self, tables):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "GIGA"}):
            return extract_pin_package_info_from_table_candidates(tables)

    def test_vendor_aliases_and_other_profiles_unchanged(self):
        for value in ["Giga", "GigaDevice", "GIGA DEVICE", "GIGA_DEVICE", "GIGA-DEVICE"]:
            self.assertEqual(get_vendor_profile(value).canonical_name, "GIGA")
        self.assertIsNone(get_vendor_profile("GIGA").source_table_repairer)
        for vendor in ["TI", "ST", "ABOV", "ELAN"]:
            self.assertNotEqual(get_vendor_profile(vendor).package_scope_resolver, resolve_giga_scopes)

    def test_explicit_package_titles_keep_all_four_independent_tables(self):
        titles = ["Table 1. Pin Description for SOP8/VSOP8/DIP8 package",
                  "Table 2. Pin Description for WSON8 package",
                  "Table 3. Pin Description for SOP16 package",
                  "Table 4 Pin Description for TFBGA24 4\\*6package"]
        rows = [[["1", "SIGNAL_A", "I", "First"]], [["1", "SIGNAL_B", "O", "Second"]],
                [["7", "SIGNAL_C", "I/O", "Third"]], [["C2", "SIGNAL_D", "I", "Fourth"]]]
        result = self.extract([table(title, self.headers, data) for title, data in zip(titles, rows)])
        self.assertEqual(len(result), 4)
        self.assertEqual([pins(pkg)[0]["pin_name"] for pkg in result], ["SIGNAL_A", "SIGNAL_B", "SIGNAL_C", "SIGNAL_D"])

    def test_pin_and_ball_tables_stay_separate_and_keep_source_multiple_nc(self):
        result = self.extract([
            table("Table 1. Pin Description for SOP8/USON8/WSON8 Package", self.headers,
                  [["1", "CS#", "I", "Select"], ["8", "VCC", "", "Supply"]]),
            table("Table 2. Ball Description for WLCSP (3-2-3 functional ball array) Package",
                  ["Ball No.", "Ball Name", "I/O", "Description"],
                  [["B3", "CS#", "I", "Select"], ["B1", "VCC", "", "Supply"], ["Multiple", "NC", "", "No Connection"]]),
        ])
        self.assertEqual([len(pins(pkg)) for pkg in result], [2, 3])
        self.assertEqual([p["pin_no"] for p in pins(result[0])], ["1", "8"])
        self.assertEqual([p["pin_no"] for p in pins(result[1])], ["B3", "B1", "Multiple"])

    def test_wlcsp_keeps_dnu_and_source_multiple_nc(self):
        result = self.extract([table("Table 4. Pin Description for WLCSP (4-4 ball array) Package", self.headers,
                                     [["A3", "CS#", "I", "Select"], ["B1", "DNU", "", "Must be left floating"],
                                      ["Multiple", "NC", "", "No Connection"]])])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])], [("A3", "CS#"), ("B1", "DNU"), ("Multiple", "NC")])

    def test_explicit_nc_pad_and_other_multiple_name_are_not_globally_removed(self):
        profile = get_vendor_profile("GIGA")
        self.assertIsNone(profile.record_filterer)
        for record in [{"pin_no": "A1", "pin_name": "NC"}, {"pin_no": "EPAD", "pin_name": "EPAD"},
                       {"pin_no": "B1", "pin_name": "DNU"}, {"pin_no": "Multiple", "pin_name": "SIGNAL"}]:
            self.assertTrue(profile.should_keep_record(record))
        self.assertTrue(profile.should_keep_record({"pin_no": "Multiple", "pin_name": "N.C."}))
        self.assertTrue(profile.should_keep_record({"pin_no": "-", "pin_name": "EPAD"}))

    def test_ambiguous_ball_name_uses_coordinate_values_and_pin_name_anchor(self):
        result = self.extract([table("PIN DESCRIPTION", ["Pin Name", "Ball Name", "I/O", "Description"],
                                     [["CS#", "A3", "I", "Select"], ["VCC", "A2", "", "Supply"]])])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])], [("A3", "CS#"), ("A2", "VCC")])

    def test_normal_ball_name_column_is_still_signal_name(self):
        headers = ["Ball No.", "Ball Name", "I/O", "Description"]
        rows = [["A3", "CS#", "I", "Select"], ["A2", "VCC", "", "Supply"]]
        fixed_headers, fixed_rows = repair_giga_table("Ball Description", headers, rows)
        self.assertEqual((fixed_headers, fixed_rows), (headers, rows))
        result = self.extract([table("Ball Description", headers, rows)])
        self.assertEqual(pins(result[0])[0]["pin_name"], "CS#")

    def test_symbolic_ball_name_without_coordinates_not_reclassified(self):
        headers = ["Pin Name", "Ball Name", "I/O"]
        rows = [["DATA", "SIGNAL", "I"], ["CLOCK", "REFERENCE", "O"]]
        self.assertEqual(repair_giga_table("Pin description", headers, rows), (headers, rows))
        self.assertIsNone(match_giga_table("Pin description", headers, rows))

    def test_package_columns_keep_their_own_pin_sets_and_epad(self):
        headers = ["Pin No. CSP4", "Pin No. SOT23-5", "Pin No. XDFN4", "Pin Name", "Description"]
        result = self.extract([table("PIN FUNCTION DESCRIPTION", headers,
                                     [["A1", "1", "4", "IN", "Input"], ["B1", "3", "3", "EN", "Enable"],
                                      ["-", "-", "EPAD", "EPAD", "Thermal pad"]])])
        self.assertEqual(len(result), 3)
        self.assertEqual([pkg["pkg"] for pkg in result], ["CSP4", "SOT23-5", "XDFN4"])
        self.assertEqual([[p["pin_no"] for p in pins(pkg)] for pkg in result], [["A1", "B1", "-"], ["1", "3", "-"], ["4", "3", "EPAD"]])

    def test_repeated_pin_functions_are_not_aggregated(self):
        result = self.extract([table("Table 1. Pin Description for SOP8 package", self.headers,
                                     [["1", "FIRST", "I", "Input"], ["1", "SECOND", "O", "Output"]])])
        self.assertEqual([(p["pin_no"], p["pin_name"], p["type"], p["description"]) for p in pins(result[0])],
                         [("1", "FIRST", "I", "Input"), ("1", "SECOND", "O", "Output")])

    def test_source_composite_signal_name_is_not_arbitrarily_split(self):
        result = self.extract([table("Table 1. Pin Description for SOP8 package", self.headers,
                                     [["7", "HOLD#/RESET# (IO3)", "I/O", "Hold or reset"]])])
        self.assertEqual(pins(result[0])[0]["pin_name"], "HOLD#/RESET# (IO3)")
        self.assertEqual(pins(result[0])[0]["type"], "I/O")

    def test_same_explicit_package_continuations_share_slot(self):
        result = self.extract([
            table("Table 1. Pin Description for TFBGA24 Package", self.headers, [["A4", "RESET#", "I", "Reset"]]),
            table("Table 1. Pin Description for TFBGA24 Package (continued)", self.headers, [["B2", "SCLK", "I", "Clock"]]),
            table("Table 2. Pin Description for SOP8 package", self.headers, [["1", "CS#", "I", "Select"]]),
        ])
        self.assertEqual([len(pins(pkg)) for pkg in result], [2, 1])

    def test_ball_array_qualifier_is_part_of_scope(self):
        result = self.extract([
            table("Table 1. Ball Description for WLCSP (3-2-3 ball array) Package", self.headers, [["B3", "FIRST", "I", "First"]]),
            table("Table 2. Ball Description for WLCSP (4-4 ball array) Package", self.headers, [["A3", "SECOND", "O", "Second"]]),
        ])
        self.assertEqual(len(result), 2)
        self.assertNotEqual(result[0]["pkg"], result[1]["pkg"])

    def test_no_title_no_evidence_does_not_force_multiple_slots(self):
        targets = [SimpleNamespace(table_id=i, title="Pin Description", headers=self.headers) for i in range(2)]
        plans = {i: SimpleNamespace(is_multi_package=False, mode="single_package") for i in range(2)}
        self.assertIsNone(resolve_giga_scopes(targets, plans))

    def test_strict_title_package_anchors(self):
        self.assertEqual(title_package("Table 3. Ball in Description for TFBGA24 Package"), "TFBGA24")
        self.assertEqual(title_package("Table 2 Ball Description for TFBGA24 5\\*5package"), "TFBGA24 5*5")
        self.assertEqual(title_package("Table 4. Pin Description for WLCSP(4-4 ball array) Package"), "WLCSP(4-4 ball array)")
        self.assertEqual(title_package("Package dimensions SOP8"), "")

    def test_non_pin_tables_do_not_match(self):
        self.assertIsNone(match_giga_table("Block sector architecture", ["Block", "Address", "Description"], [["0", "0000", "Memory"]]))


if __name__ == "__main__":
    unittest.main()
