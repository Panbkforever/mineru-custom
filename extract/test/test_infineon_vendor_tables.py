"""验证 Infineon 厂家隔离、表头别名、输出配置及复用功能保留。"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from extract.pin_package_extractor import TableCandidate, extract_pin_package_info_from_table_candidates
from extract.test.test_giga_vendor_tables import table, pins
from extract.vendors import get_vendor_profile
from extract.vendors.infineon.package_handlers import output_scope, resolve_infineon_scopes
from extract.vendors.infineon.table_handlers import (
    match_infineon_table, prepare_infineon_tables, is_mode_comparison,
)


class InfineonVendorTableTest(unittest.TestCase):
    def extract(self, tables):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "INFINEON"}):
            return extract_pin_package_info_from_table_candidates(tables)

    def test_registry_and_vendor_isolation(self):
        self.assertEqual(get_vendor_profile("Infineon").canonical_name, "INFINEON")
        for vendor in ("TI", "ST", "ABOV", "ELAN", "GIGA"):
            self.assertNotEqual(get_vendor_profile(vendor).source_table_repairer, prepare_infineon_tables)

    def test_designation_without_type_and_description(self):
        result = self.extract([table("Pin Designation", ["Pin #", "Designation"],
                                     [["1", "+ Input"], ["2", "Input Return"], ["12", "Enable 2"]])])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])],
                         [("1", "+ Input"), ("2", "Input Return"), ("12", "Enable 2")])

    def test_number_header_and_designation_without_pin_title(self):
        result = self.extract([table("Single Output", ["Pin Number", "Designation"], [["5", "- Sense"]])])
        self.assertEqual(pins(result[0])[0]["pin_name"], "- Sense")

    def test_single_and_dual_outputs_are_independent(self):
        result = self.extract([
            table("Table 7 Single Output", ["Pin Number", "Designation"], [["1", "Case Ground"], ["5", "- Sense"]]),
            table("Dual Output", ["Pin Number", "Designation"], [["1", "Case Ground"], ["5", "NC"]]),
        ])
        self.assertEqual([len(pins(p)) for p in result], [2, 2])
        self.assertEqual([pins(p)[1]["pin_name"] for p in result], ["- Sense", "NC"])

    def test_same_scope_continuations_share_slot(self):
        result = self.extract([
            table("Single Output", ["Pin #", "Designation"], [["1", "First"]]),
            table("Single Output (cont'd)", ["Pin #", "Designation"], [["2", "Second"]]),
            table("Dual Output", ["Pin #", "Designation"], [["1", "Other"]]),
        ])
        self.assertEqual([len(pins(p)) for p in result], [2, 1])

    def test_voltage_specification_title_is_not_pin_scope(self):
        self.assertEqual(output_scope("Table 7 Single Output"), "single output")
        self.assertEqual(output_scope("Dual Output (continued)"), "dual output")
        self.assertEqual(output_scope("Single Output Voltage Ranges, Constants and Resistor connection by Model"), "")
        self.assertEqual(output_scope("Single Output Electrical Characteristics"), "")

    def test_unknown_sections_do_not_force_independent_slots(self):
        targets = [SimpleNamespace(table_id=0, title="Port Interface", headers=["Pin number", "Pin name"])]
        plans = {0: SimpleNamespace(is_multi_package=False)}
        self.assertIsNone(resolve_infineon_scopes(targets, plans))

    def test_pin_type_does_not_use_buffer_type(self):
        result = self.extract([table("Port Interface", ["Pin or Ball No.", "Name", "Pin Type", "Buffer Type", "Function"],
                                     [["40", "TXP", "AI/O", "TTL PD 8mA", "Transmit"]])])
        self.assertEqual(pins(result[0])[0], {"pin_no": "40", "pin_name": "TXP", "type": "AI/O", "description": "Transmit"})

    def test_rowspan_functions_remain_separate_records(self):
        html = ('<table><tr><td>Pin or Ball No.</td><td>Name</td><td>Pin Type</td><td>Function</td></tr>'
                '<tr><td rowspan="2">87</td><td>TXD0</td><td rowspan="2">I/O</td><td>Transmit</td></tr>'
                '<tr><td>MODE0</td><td>Configure</td></tr></table>')
        result = self.extract([TableCandidate(html=html, title="Port Interface", page_idx=0)])
        self.assertEqual([(p["pin_no"], p["pin_name"], p["type"]) for p in pins(result[0])],
                         [("87", "TXD0", "I/O"), ("87", "MODE0", "I/O")])

    def test_supply_list_expands_without_changing_shared_name(self):
        result = self.extract([table("Ground/Power Interface", ["Pin or Ball No.", "Name", "Pin Type", "Function"],
                                     [["42, 48", "GNDTR", "GND, A", "Ground"]])])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])], [("42", "GNDTR"), ("48", "GNDTR")])

    def test_interface_comparison_does_not_create_duplicate_pins(self):
        comparison = table("Table 12 Port 2 MII/RMII/GPSI/HDLC Interfaces Comparison",
                           ["Pin No.", "MII", "RMII", "GPSI", "HDLC"], [["87", "TXD0", "TXD0", "TXD", "TXD"]])
        result = self.extract([table("Pin Description", ["Pin No.", "Name", "Pin Type"], [["87", "TXD0", "I/O"]]), comparison])
        self.assertEqual(len(pins(result[0])), 1)
        self.assertTrue(is_mode_comparison(comparison.title, ["Pin No.", "MII", "RMII"]))

    def test_unrelated_comparison_and_physical_tables_are_not_removed(self):
        self.assertFalse(is_mode_comparison("Voltage Comparison", ["Pin No.", "MII", "RMII"]))
        self.assertFalse(is_mode_comparison("Interfaces Comparison", ["Pin No.", "Pin Name", "Type"]))
        self.assertIsNone(match_infineon_table("Abbreviations", ["Abbreviations", "Description"], [["I", "Input"]]))

    def test_unknown_header_and_metadata_are_preserved(self):
        candidate = table("Electrical Characteristics", ["Parameter", "Value"], [["Voltage", "3.3"]], page=4)
        self.assertIs(prepare_infineon_tables([candidate])[0], candidate)
        candidate = table("Pin Designation", ["Pin #", "Designation"], [["1", "Input"]], page=4)
        candidate.group_context = "Section context"
        fixed = prepare_infineon_tables([candidate])[0]
        self.assertEqual((fixed.page_idx, fixed.title, fixed.group_context), (4, "Pin Designation", "Section context"))

    def test_no_nc_invented_from_gap_in_numbers(self):
        result = self.extract([table("Pin Description", ["Pin No.", "Name", "Pin Type"],
                                     [["1", "DATA", "I/O"], ["4", "VDD", "PWR"]])])
        self.assertEqual([p["pin_no"] for p in pins(result[0])], ["1", "4"])

    def test_explicit_nc_is_kept(self):
        result = self.extract([table("Pin Description", ["Pin No.", "Name", "Pin Type"], [["5, 31", "NC", ""]])])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])], [("5", "NC"), ("31", "NC")])


if __name__ == "__main__":
    unittest.main()
