import unittest
from types import SimpleNamespace
from unittest.mock import patch

from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
    split_pin_numbers,
)
from extract.vendors.registry import get_vendor_profile
from extract.vendors.st.package_handlers import resolve_st_package_scopes
from extract.vendors.st.table_handlers import (
    _package_label_from_pin_header,
    repair_st_table,
    repair_st_table_rows,
    should_keep_st_record,
)


def table(title, headers, rows, page=0):
    html_rows = [headers] + rows
    html = "<table>" + "".join(
        "<tr>" + "".join("<td>" + value + "</td>" for value in row) + "</tr>"
        for row in html_rows
    ) + "</table>"
    return TableCandidate(html=html, page_idx=page, title=title)


def pins(package):
    return [pin for group in package["group_list"] for pin in group["pin_list"]]


class STPinTableRepairTest(unittest.TestCase):
    def extract(self, tables):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "ST"}):
            return extract_pin_package_info_from_table_candidates(tables)

    def test_exposed_pad_remains_one_pin_in_package_column(self):
        result = self.extract([table(
            "Table 4. Pin descriptions",
            ["Pin number QFN32", "Pin number WLCSP36", "Pin name", "Pin type", "Description"],
            [["1", "A1", "SIGNAL", "I/O", "Signal"],
             ["Exposed pad", "-", "GND", "S", "Ground"]],
        )])
        self.assertEqual([len(pins(pkg)) for pkg in result], [2, 1])
        self.assertEqual(pins(result[0])[-1], {"pin_no": "EP", "pin_name": "GND", "type": "S", "description": "Ground"})

    def test_pad_repair_does_not_join_explicit_pin_lists(self):
        headers = ["Pin number QFN32", "Pin name", "Pin type"]
        rows = [["3 6", "VSS", "S"], ["Thermal pad", "GND", "S"]]
        fixed = repair_st_table_rows("Table 4. Pin assignment", headers, rows)
        self.assertEqual(fixed[0][0], "3 6")
        self.assertEqual(split_pin_numbers(fixed[0][0]), ["3", "6"])
        self.assertEqual(fixed[1][0], "EP")

    def test_na_filter_preserves_real_thermal_pad_and_unknown_name(self):
        for value in ["NA", "N/A", "N.A.", "-", "--"]:
            self.assertFalse(should_keep_st_record({"pin_no": value, "pin_name": "GND"}))
        self.assertTrue(should_keep_st_record({"pin_no": "33", "pin_name": "NA"}))
        self.assertTrue(should_keep_st_record({"pin_no": "N1", "pin_name": "NA"}))

    def test_plain_assignment_headers_pass_initial_candidate_filter(self):
        result = self.extract([table(
            "Table 3. IC200B VFQFPN32 pin assignment",
            ["VFQFPN32", "Name", "$Type^{(1)}$", "Description"],
            [["1", "VDD", "P", "Supply"], ["33", "NA", "P", "Thermal pad (GND)"]],
        )])
        self.assertEqual(len(result), 1)
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])], [("1", "VDD"), ("33", "NA")])

    def test_disjoint_model_tables_keep_independent_package_mappings(self):
        result = self.extract([
            table("Table 2. IC100A - VFQFPN32, UFQFPN32 and WLCSP36 pin assignment",
                  ["VFQFPN32", "WLCSP", "Name", "Type(1)", "Description"],
                  [["1", "A1", "SIGNAL_A", "I/O", "First model signal"],
                   ["33", "NA", "NA", "P", "Thermal pad (GND)"]]),
            table("Table 3. IC200B/IC200C VFQFPN32 and UFQFPN32 pin assignment",
                  ["VFQFPN32", "Name", "Type(1)", "Description"],
                  [["1", "SIGNAL_B", "I/O", "Second model signal"]], 1),
            table("Table 3. IC200C/IC200B VFQFPN32 and UFQFPN32 pin assignment (continued)",
                  ["VFQFPN32", "Name", "Type(1)", "Description"],
                  [["33", "NA", "P", "Thermal pad (GND)"]], 2),
        ])
        self.assertEqual([len(pins(pkg)) for pkg in result], [2, 2, 1])
        self.assertEqual(pins(result[0])[0]["pin_name"], "SIGNAL_A")
        self.assertEqual(pins(result[1])[0]["pin_name"], "SIGNAL_B")
        self.assertEqual(pins(result[2])[0]["pin_no"], "A1")
        self.assertEqual(len({pkg["pkg"] for pkg in result}), 3)

    def test_overlapping_model_scopes_do_not_force_binding(self):
        targets = [SimpleNamespace(
            table_id=index, title=title,
            headers=["Pin number VFQFPN32", "Pin name", "Pin type"],
        ) for index, title in enumerate([
            "Table 2. IC100A VFQFPN32 pin assignment",
            "Table 3. IC100A/IC200B VFQFPN32 pin assignment",
        ])]
        plans = {index: SimpleNamespace(is_multi_package=False, mode="single_package") for index in range(2)}
        self.assertIsNone(resolve_st_package_scopes(targets, plans))

    def test_footnoted_header_recovers_all_package_columns_and_missing_name(self):
        labels = ["WLCSP80 SMPS", "LQFP100 SMPS", "LQFP144 SMPS", "UFBGA169 SMPS",
                  "LQFP176 SMPS", "UFBGA176+25 SMPS", "LQFP64", "LQFP100", "LQFP144",
                  "UFBGA169", "LQFP176", "UFBGA176+25", "VFQFPN68"]
        headers = ["Pin number(1)(2)"] * 12 + ["Pin name (function after reset)(3)(4)",
                    "Pin type", "I/O structure", "Notes", "Alternate functions", "Additional functions", ""]
        label_row = labels + ["Pin type", "I/O structure", "Notes", "Alternate functions", "Additional functions", ""]
        data = [["-", "-", "12", "F4", "18", "G3", "-", "-", "12", "F2", "18", "H2", "-", "PF2", "I/O", "FT_h", "-", "EVENTOUT", "-"],
                ["-", "-", "13", "G6", "19", "G2", "-", "-", "13", "F5", "19", "J2", "-", "PF3", "I/O", "FT_h", "-", "EVENTOUT", "-"]]
        fixed_headers, fixed_rows = repair_st_table("Table 14. pin/ball definition (continued)", headers, [label_row] + data)
        self.assertEqual(fixed_headers[:13], ["Pin number " + value for value in labels])
        self.assertTrue(fixed_headers[13].startswith("Pin name"))
        self.assertEqual(fixed_headers[14], "Pin type")
        self.assertEqual(len(fixed_headers), 19)
        self.assertEqual(fixed_rows, data)

    def test_package_label_keeps_additive_count_and_removes_only_footnotes(self):
        self.assertEqual(_package_label_from_pin_header("Pin number(1)(2) UFBGA176+25 SMPS"), "UFBGA176+25 SMPS")
        self.assertEqual(_package_label_from_pin_header("Pin number VFQFPN68"), "VFQFPN68")

    def test_non_bga_pin_definition_repairs_wrapped_numbers_with_evidence(self):
        headers = ["Pin number UFQFPN32", "Pin number LQFP48", "Pin name", "Pin type"]
        rows = [["9", "13", "PA3", "I/O"], ["1\n0", "1\n4", "PA4", "I/O"], ["11", "15", "PA5", "I/O"]]
        fixed = repair_st_table_rows("Table 10. MCU pin/ball definition", headers, rows)
        self.assertEqual(fixed[1][:2], ["10", "14"])

    def test_non_bga_repair_preserves_ambiguous_numeric_lines(self):
        headers = ["Pin number LQFP48", "Pin name", "Pin type"]
        rows = [["2", "PA0", "I/O"], ["3\n6", "VSS", "S"], ["7", "PA1", "I/O"]]
        fixed = repair_st_table_rows("Table 10. MCU pin/ball definition", headers, rows)
        self.assertEqual(fixed[1][0], "3\n6")

    def test_parallel_pin_names_override_numeric_join_evidence(self):
        headers = ["Pin number LQFP48", "Pin number LQFP64", "Pin number LQFP100", "Pin name", "Pin type"]
        rows = [["30", "3\n6", "40", "PA1\nPA2", "I/O"]]
        fixed = repair_st_table_rows("Table 10. MCU pin/ball definition", headers, rows)
        self.assertEqual(fixed[0][1], "3\n6")

    def test_malformed_multilevel_header_is_repaired_through_full_entry(self):
        labels = ["WLCSP80 SMPS", "LQFP100 SMPS", "LQFP144 SMPS", "UFBGA169 SMPS",
                  "LQFP176 SMPS", "UFBGA176+25 SMPS", "LQFP64", "LQFP100", "LQFP144",
                  "UFBGA169", "LQFP176", "UFBGA176+25", "VFQFPN68"]
        headers = ["Pin number(1)(2)"] * 12 + ["Pin name (function after reset)(3)(4)",
                   "Pin type", "I/O structure", "Notes", "Alternate functions", "Additional functions", ""]
        label_row = labels + ["Pin type", "I/O structure", "Notes", "Alternate functions", "Additional functions", ""]
        first = ["1", "1", "1", "A1", "1", "A1", "1", "1", "1", "A1", "1", "A1", "1", "PA0", "I/O", "FT", "-", "-", "-"]
        second = ["2", "2", "2", "A2", "2", "A2", "2", "2", "2", "A2", "2", "A2", "2", "PA1", "I/O", "FT", "-", "-", "-"]
        result = self.extract([table("Table 14. MCU pin/ball definition (continued)", headers, [label_row, first, second])])
        self.assertEqual(len(result), 13)
        for package in result:
            self.assertEqual([pin["pin_name"] for pin in pins(package)], ["PA0", "PA1"])

    def test_ti_profile_has_no_st_repairs_or_scope_override(self):
        profile = get_vendor_profile("TI")
        self.assertIsNone(profile.package_scope_resolver)
        headers = ["Pin number QFN32", "Pin name", "Pin type"]
        rows = [["Exposed pad", "GND", "P"]]
        self.assertIs(profile.repair_rows("Pin descriptions", headers, rows), rows)


if __name__ == "__main__":
    unittest.main()
