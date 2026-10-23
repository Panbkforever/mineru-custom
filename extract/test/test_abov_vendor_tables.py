import unittest
from html import escape
from unittest.mock import patch

from extract.pin_package_extractor import TableCandidate, extract_pin_package_info_from_table_candidates
from extract.vendors import get_vendor_profile, supported_vendors
from extract.vendors.abov.table_handlers import (
    _numbers, match_abov_table, repair_abov_rows, repair_abov_table,
)


def table(headers, rows, title="Pin descriptions"):
    html = "<table>" + "".join(
        "<tr>" + "".join("<td>" + escape(v).replace("\n", "<br>") + "</td>" for v in row) + "</tr>"
        for row in [headers] + rows
    ) + "</table>"
    return TableCandidate(html=html, page_idx=0, title=title)


def pins(package):
    return [p for group in package["group_list"] for p in group["pin_list"]]


class ABOVVendorTableTest(unittest.TestCase):
    def extract(self, headers, rows, title="Pin descriptions"):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "ABOV"}):
            return extract_pin_package_info_from_table_candidates([table(headers, rows, title)])

    def test_registered_without_overriding_ti_or_st(self):
        self.assertIn("ABOV", supported_vendors())
        self.assertEqual(get_vendor_profile("abov").canonical_name, "ABOV")
        self.assertIsNone(get_vendor_profile("TI").row_repairer)
        self.assertEqual(get_vendor_profile("ST").row_repairer.__module__, "extract.vendors.st.table_handlers")

    def test_variants_keep_distinct_package_columns_and_separate_functions(self):
        headers = ["Pin number QFN-32(1)", "Pin number QFN-32(2)", "Pin name", "Type", "Description"]
        rows = [["1", "2", "PA0", "I/O", "GPIO"],
                ["1", "2", "AN0", "IA", "Analog"],
                ["-", "1", "VDD", "P", "Supply"]]
        result = self.extract(headers, rows)
        self.assertEqual(len(result), 2)
        self.assertEqual([len(pins(p)) for p in result], [2, 3])
        self.assertEqual([(p["pin_no"], p["pin_name"], p["type"], p["description"])
                          for p in pins(result[0])],
                         [("1", "PA0", "I/O", "GPIO"), ("1", "AN0", "IA", "Analog")])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[1])],
                         [("2", "PA0"), ("2", "AN0"), ("1", "VDD")])
        self.assertNotEqual(result[0]["pkg"], result[1]["pkg"])

    def test_package_footnote_is_not_invented_as_variant(self):
        headers = ["Pin number LQFP-32(1)", "Pin name", "Type"]
        fixed, _ = repair_abov_table("Pins", headers, [["1", "PA0", "I/O"]])
        self.assertEqual(fixed, headers)

    def test_shared_exact_name_dedup_still_preserves_description_supplements(self):
        result = self.extract(["Pin #", "Pin Name", "I O", "Description"],
                              [["1", "MODE", "I", "Operating mode"],
                               ["1", "MODE", "I", "Test mode"],
                               ["2", "VSS", "P", "Ground"]])
        self.assertEqual(len(pins(result[0])), 2)
        self.assertIn("Test mode", pins(result[0])[0]["description"])

    def test_explicit_numeric_range_syntax_and_direction(self):
        self.assertEqual(_numbers("3 to 5"), ["3", "4", "5"])
        self.assertEqual(_numbers("3\u20135"), ["3", "4", "5"])
        self.assertEqual(_numbers("7 - 5"), ["7", "6", "5"])
        self.assertEqual(_numbers("1,\n4\u20136"), ["1", "4", "5", "6"])

    def test_nested_descriptions_do_not_merge_different_function_rows(self):
        result = self.extract(["Pin #", "Pin Name", "I O", "Description", "Description"],
                              [["1", "DATA0/BOOT", "I", "Mode", "Normal"],
                               ["1", "DATA0/BOOT", "I", "Mode", "Test"],
                               ["1", "GPIO0", "I/O", "GPIO", "GPIO"],
                               ["1", "DATA0/BOOT", "I", "Mode", "Normal"]])
        records = pins(result[0])
        self.assertEqual([(p["pin_no"], p["pin_name"], p["type"]) for p in records],
                         [("1", "DATA0/BOOT", "I"), ("1", "GPIO0", "I/O")])
        self.assertIn("Normal", records[0]["description"])
        self.assertIn("Test", records[0]["description"])
        self.assertEqual(records[1]["description"], "GPIO")

    def test_same_number_keeps_each_name_type_and_description_as_own_record(self):
        headers = ["Pin number LQFP-48", "Pin name", "Type", "Description"]
        rows = [["1", "PA5*", "IOUDS", "PORT A Bit 5 Input/Output"],
                ["1", "AN5", "IA", "A/D Converter Analog Input 5"],
                ["1", "T12OUT", "O", "Timer 12 Pulse Output"],
                ["1", "T12CAP", "I", "Timer 12 Capture Input"]]
        result = self.extract(headers, rows)
        self.assertEqual(pins(result[0]), [
            dict(zip(["pin_no", "pin_name", "type", "description"], row)) for row in rows
        ])

    def test_row_repair_does_not_aggregate_rows_with_same_number_and_name(self):
        headers = ["Pin #", "Pin Name", "Type", "Description"]
        rows = [["1", "SIGNAL", "I", "Input mode"],
                ["1", "SIGNAL", "O", "Output mode"],
                ["1", "ALTERNATE", "", "No type specified"]]
        self.assertEqual(repair_abov_rows("Pin descriptions", headers, rows), rows)

    def test_unknown_expression_and_oversized_range_not_guessed(self):
        for value in ["1-5(6)", "1abc", "3 to", "0", "1 to 5000", "2 3"]:
            self.assertIsNone(_numbers(value))

    def test_to_range_preserves_parallel_signal_aliases(self):
        result = self.extract(["PIN NAME", "I/O", "DESCRIPTION", "PIN No."],
                              [["SG1/KS1 to\nSG3/KS3", "O", "Segment", "10 to 12"]])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])],
                         [("10", "SG1/KS1"), ("11", "SG2/KS2"), ("12", "SG3/KS3")])

    def test_range_names_only_expand_when_cardinality_agrees(self):
        result = self.extract(["Pin Name", "Pin No.", "Type"], [["SG1-SG3", "1-2", "O"]])
        self.assertEqual([p["pin_name"] for p in pins(result[0])], ["SG1-SG3"] * 2)

    def test_multiline_ranges_keep_group_order_and_names(self):
        result = self.extract(["Pin Names", "I/O", "Pin Description", "Pin No."],
                              [["SEG1 - SEG2\nSEG3 - SEG5", "O", "Segment", "8 - 9\n1 - 3"],
                               ["COM1\nCOM2", "O", "Common", "6, 10\n5, 11"],
                               ["VDD", "-", "Supply", "12,\n13\u201315"]])
        by_pin = {p["pin_no"]: p["pin_name"] for p in pins(result[0])}
        self.assertEqual([by_pin[str(i)] for i in [8, 9, 1, 2, 3]], ["SEG1", "SEG2", "SEG3", "SEG4", "SEG5"])
        self.assertEqual([by_pin[str(i)] for i in [6, 10, 5, 11]], ["COM1", "COM1", "COM2", "COM2"])
        self.assertTrue(all(by_pin[str(i)] == "VDD" for i in range(12, 16)))

    def test_wrapped_digits_require_adjacent_sequence_evidence(self):
        headers = ["Pin #", "Pin Name", "Type"]
        rows = [["22", "DATA0", "I/O"], ["2\n3", "DATA1", "I/O"], ["24", "DATA2", "I/O"]]
        fixed = repair_abov_rows("Pin List", headers, rows)
        self.assertEqual([r[0] for r in fixed], ["22", "23", "24"])

    def test_consecutive_wrapped_numbers_use_both_outer_anchors(self):
        headers = ["Pin #", "Pin Name", "Type"]
        rows = [["109", "SIG0", "I/O"], ["1\n10", "SIG1", "I/O"],
                ["1\n11", "SIG2", "I/O"], ["112", "SIG3", "I/O"]]
        self.assertEqual([r[0] for r in repair_abov_rows("Pin List", headers, rows)], ["109", "110", "111", "112"])

    def test_ambiguous_wrapped_digits_are_not_concatenated(self):
        headers = ["Pin #", "Pin Name", "Type"]
        rows = [["2", "GND", "P"], ["3\n6", "GND", "P"], ["7", "GND", "P"]]
        self.assertNotIn("36", [r[0] for r in repair_abov_rows("Pin List", headers, rows)])

    def test_explicit_parallel_names_prevent_concatenation(self):
        headers = ["Pin #", "Pin Name", "Type"]
        rows = [["22", "A0", "I"], ["2\n3", "A1\nA2", "I"], ["24", "A3", "I"]]
        result = repair_abov_rows("Pin List", headers, rows)
        self.assertEqual([(r[0], r[1]) for r in result[1:3]], [("2", "A1"), ("3", "A2")])

    def test_parenthesized_permutation_is_two_mappings(self):
        result = self.extract(["Pin Names", "Pin No.", "I/O", "Pin Description"],
                              [["S1-S3", "1-3(5,6,1)", "O", "Segment"],
                               ["COM1-COM2", "5-4(3-2)", "O", "Common"],
                               ["VDD", "6(4)", "P", "Supply"]])
        self.assertEqual(len(result), 2)
        self.assertEqual([len(pins(p)) for p in result], [6, 6])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[1])][:3], [("5", "S1"), ("6", "S2"), ("1", "S3")])
        for package in result:
            self.assertFalse(any("__PIN_RANGE" in p["pin_no"] for p in pins(package)))

    def test_numeric_parenthesis_footnotes_do_not_create_package(self):
        headers = ["Pin Names", "Pin No.", "I/O"]
        rows = [["A", "1(1)", "I"], ["B", "2(1)", "I"], ["C", "3(1)", "I"]]
        fixed_headers, fixed_rows = repair_abov_table("Pin description", headers, rows)
        self.assertEqual((fixed_headers, fixed_rows), (headers, rows))

    def test_function_only_and_electrical_tables_do_not_match(self):
        self.assertIsNone(match_abov_table("Peripheral pins", ["Pin Name", "Type", "Description"], [["TXD", "O", "UART"]]))
        self.assertIsNone(match_abov_table("Characteristics", ["Symbol", "Min", "Max"], [["VDD", "1", "5"]]))

    def test_wrapped_number_is_checked_by_physical_groups_not_function_rows(self):
        headers = ["Pin number LQFP-64", "Pin name", "Type"]
        rows = [["56", "PREV", "I"], ["56", "PREV_ALT", "O"],
                ["5\n7", "SIGNAL", "I/O"], ["5\n7", "SIGNAL_ALT", "I"],
                ["58", "NEXT", "I"], ["58", "NEXT_ALT", "O"]]
        fixed = repair_abov_rows("Pins", headers, rows)
        self.assertEqual([r[0] for r in fixed], ["56", "56", "57", "57", "58", "58"])
        self.assertEqual([r[1:] for r in fixed], [r[1:] for r in rows])

    def test_wrapped_last_group_uses_two_parallel_single_mapping_anchors(self):
        headers = ["Pin number LQFP-64", "Pin number MQFP-60", "Pin name", "Type"]
        rows = [["56", "52", "PREV", "I"], ["57", "53", "NEAR", "I/O"],
                ["57", "53", "NEAR_ALT", "O"], ["5\n8", "54", "LAST", "I/O"],
                ["5\n8", "54", "LAST_ALT", "I"]]
        fixed = repair_abov_rows("Pins", headers, rows)
        self.assertEqual([r[0] for r in fixed], ["56", "57", "57", "58", "58"])
        self.assertEqual([r[1:] for r in fixed], [r[1:] for r in rows])

    def test_wrapped_first_group_can_use_parallel_mapping_evidence(self):
        headers = ["Pin number LQFP-64", "Pin number MQFP-60", "Pin name", "Type"]
        rows = [["5\n6", "52", "FIRST", "I"], ["57", "53", "NEXT", "I"], ["58", "54", "AFTER", "I"]]
        self.assertEqual([r[0] for r in repair_abov_rows("Pins", headers, rows)], ["56", "57", "58"])

    def test_multi_package_fragment_preserves_function_membership(self):
        headers = ["Pin number LQFP-64", "Pin number MQFP-60", "Pin name", "Type"]
        rows = [["56", "52", "PREV", "I"], ["5\n7", "53", "GPIO", "I/O"],
                ["5\n7", "53", "ADC", "IA"], ["58", "54", "NEXT", "O"]]
        result = self.extract(headers, rows)
        self.assertEqual(len(result), 2)
        self.assertEqual([(p["pin_no"], p["pin_name"], p["type"]) for p in pins(result[0])],
                         [("56", "PREV", "I"), ("57", "GPIO", "I/O"), ("57", "ADC", "IA"), ("58", "NEXT", "O")])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[1])],
                         [("52", "PREV"), ("53", "GPIO"), ("53", "ADC"), ("54", "NEXT")])

    def test_one_parallel_anchor_is_not_enough_at_boundary(self):
        headers = ["Pin number LQFP-64", "Pin number MQFP-60", "Pin name", "Type"]
        rows = [["57", "53", "PREV", "I"], ["5\n8", "54", "LAST", "I"]]
        self.assertNotIn("58", [r[0] for r in repair_abov_rows("Pins", headers, rows)])

    def test_repeated_function_rows_do_not_count_as_two_mapping_anchors(self):
        headers = ["Pin number LQFP-64", "Pin number MQFP-60", "Pin name", "Type"]
        rows = [["57", "53", "PREV", "I"], ["57", "53", "PREV_ALT", "O"],
                ["5\n8", "54", "LAST", "I"]]
        self.assertNotIn("58", [r[0] for r in repair_abov_rows("Pins", headers, rows)])

    def test_conflicting_parallel_mapping_does_not_guess_boundary_number(self):
        headers = ["Pin number LQFP-64", "Pin number MQFP-60", "Pin name", "Type"]
        rows = [["56", "51", "PREV", "I"], ["57", "53", "NEAR", "I"], ["5\n8", "54", "LAST", "I"]]
        self.assertNotIn("58", [r[0] for r in repair_abov_rows("Pins", headers, rows)])

    def test_single_column_boundary_without_evidence_is_unchanged(self):
        headers = ["Pin #", "Pin name", "Type"]
        rows = [["56", "A", "I"], ["57", "B", "I"], ["5\n8", "C", "I"]]
        self.assertNotIn("58", [r[0] for r in repair_abov_rows("Pins", headers, rows)])

    def test_genuine_newline_number_list_is_not_blindly_joined(self):
        headers = ["Pin #", "Pin name", "Type"]
        rows = [["2", "A", "P"], ["3\n6", "GND", "P"], ["7", "B", "P"]]
        fixed = repair_abov_rows("Pins", headers, rows)
        self.assertEqual([(r[0], r[1]) for r in fixed], [("2", "A"), ("3", "GND"), ("6", "GND"), ("7", "B")])

    def test_explicit_name_range_protects_parallel_number_list(self):
        headers = ["Pin #", "Pin name", "Type"]
        rows = [["56", "A", "I"], ["5\n7", "SIG0-SIG1", "I"], ["58", "B", "I"]]
        fixed = repair_abov_rows("Pins", headers, rows)
        self.assertEqual([(r[0], r[1]) for r in fixed[1:3]], [("5", "SIG0"), ("7", "SIG1")])

    def test_descending_physical_sequence_and_consecutive_wrapped_groups(self):
        headers = ["Pin #", "Pin name", "Type"]
        rows = [["59", "A", "I"], ["5\n8", "B", "I"], ["5\n8", "B_ALT", "O"],
                ["5\n7", "C", "I"], ["56", "D", "I"]]
        self.assertEqual([r[0] for r in repair_abov_rows("Pins", headers, rows)], ["59", "58", "58", "57", "56"])


if __name__ == "__main__":
    unittest.main()
