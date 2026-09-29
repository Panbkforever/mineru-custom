import os
import unittest

from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
)
from extract.vendors.st.table_handlers import repair_st_table, repair_st_table_rows
from extract.vendors.st.table_handlers import should_keep_st_record


class STVendorPadTableTest(unittest.TestCase):
    def test_st_pad_description_table_extracts_pin_records(self):
        old_vendor = os.environ.get("EXTRACT_VENDOR")
        os.environ["EXTRACT_VENDOR"] = "ST"
        try:
            result = extract_pin_package_info_from_table_candidates(
                [
                    TableCandidate(
                        html=(
                            "<table>"
                            "<tr><td>Pad ref</td><td>Pad name</td><td>Description</td></tr>"
                            "<tr><td>A1</td><td>OUT</td><td>Antenna</td></tr>"
                            "<tr><td>A2</td><td>GND4</td><td>Ground</td></tr>"
                            "</table>"
                        ),
                        page_idx=0,
                        title="Table 5. Pad description top view (pads down)",
                    )
                ],
                source_name="st_sample",
            )
        finally:
            if old_vendor is None:
                os.environ.pop("EXTRACT_VENDOR", None)
            else:
                os.environ["EXTRACT_VENDOR"] = old_vendor

        self.assertEqual(len(result), 1)
        pins = result[0]["group_list"][0]["pin_list"]
        self.assertEqual(
            pins,
            [
                {"pin_no": "A1", "pin_name": "OUT", "description": "Antenna"},
                {"pin_no": "A2", "pin_name": "GND4", "description": "Ground"},
            ],
        )

    def test_st_pin_assignment_table_extracts_package_columns(self):
        old_vendor = os.environ.get("EXTRACT_VENDOR")
        os.environ["EXTRACT_VENDOR"] = "ST"
        try:
            result = extract_pin_package_info_from_table_candidates(
                [
                    TableCandidate(
                        html=(
                            "<table>"
                            "<tr><td>Pin</td><td>Pin</td><td>Pin name</td><td>Pin type</td></tr>"
                            "<tr><td>SO8N</td><td>WLCSP12</td><td>Pin name</td><td>Pin type</td></tr>"
                            "<tr><td>1</td><td>B3</td><td>PC14</td><td>I/O</td></tr>"
                            "<tr><td>2</td><td>C4</td><td>VDD</td><td>S</td></tr>"
                            "</table>"
                        ),
                        page_idx=0,
                        title="Table 12. Pin assignment and description",
                    )
                ],
                source_name="st_mcu_sample",
            )
        finally:
            if old_vendor is None:
                os.environ.pop("EXTRACT_VENDOR", None)
            else:
                os.environ["EXTRACT_VENDOR"] = old_vendor

        self.assertEqual(len(result), 2)
        self.assertEqual([item["pkg"] for item in result], ["SO8N", "WLCSP12"])
        self.assertEqual(result[0]["group_list"][0]["pin_list"][0]["pin_no"], "1")
        self.assertEqual(result[0]["group_list"][0]["pin_list"][0]["pin_name"], "PC14")
        self.assertEqual(result[1]["group_list"][0]["pin_list"][0]["pin_no"], "B3")
        self.assertEqual(result[1]["group_list"][0]["pin_list"][0]["pin_name"], "PC14")

    def test_st_ball_definition_table_repairs_wrapped_and_merged_cells(self):
        old_vendor = os.environ.get("EXTRACT_VENDOR")
        os.environ["EXTRACT_VENDOR"] = "ST"
        try:
            result = extract_pin_package_info_from_table_candidates(
                [
                    TableCandidate(
                        html=(
                            "<table>"
                            "<tr>"
                            "<td>Pin Number</td><td>Pin Number</td><td>Pin Number</td>"
                            "<td>Pin name<br>(function after reset)</td><td>Pin type</td>"
                            "<td>I/O structure</td><td>Notes</td><td>Ball functions</td><td>Ball functions</td>"
                            "</tr>"
                            "<tr>"
                            "<td>LFBGA289</td><td>TFBGA289</td><td>TFBGA320</td>"
                            "<td>Pin name<br>(function after reset)</td><td>Pin type</td>"
                            "<td>I/O structure</td><td>Notes</td><td>Alternate functions</td><td>Additional functions</td>"
                            "</tr>"
                            "<tr><td>L5</td><td>U<br>2</td><td>W1</td><td>PG3</td><td>I/O</td><td>FT</td><td>-</td><td>-</td><td>-</td></tr>"
                            "<tr><td>-L14</td><td>N9-</td><td>E11-</td><td>VSSDDR_DTO0</td><td>SO</td><td>-DDR</td><td>--</td><td>--</td><td>--</td></tr>"
                            "<tr><td>A9-</td><td>D9C6</td><td>C12G13</td><td>PH9VDD</td><td>I/OS</td><td>FT_h-</td><td>--</td><td>TIM1_CH4-</td><td>--</td></tr>"
                            "</table>"
                        ),
                        page_idx=0,
                        title="Table 7. STM32MP133C/F ball definitions",
                    )
                ],
                source_name="st_bga_sample",
            )
        finally:
            if old_vendor is None:
                os.environ.pop("EXTRACT_VENDOR", None)
            else:
                os.environ["EXTRACT_VENDOR"] = old_vendor

        by_pkg = {
            item["pkg"]: item["group_list"][0]["pin_list"]
            for item in result
        }
        self.assertEqual(set(by_pkg), {"LFBGA289", "TFBGA289", "TFBGA320"})
        self.assertIn({"pin_no": "U2", "pin_name": "PG3", "type": "I/O"}, by_pkg["TFBGA289"])
        self.assertIn({"pin_no": "N9", "pin_name": "VSS", "type": "S"}, by_pkg["TFBGA289"])
        self.assertIn({"pin_no": "D9", "pin_name": "PH9", "type": "I/O"}, by_pkg["TFBGA289"])
        self.assertIn({"pin_no": "C6", "pin_name": "VDD", "type": "S"}, by_pkg["TFBGA289"])
        self.assertNotIn("U", {pin["pin_no"] for pin in by_pkg["TFBGA289"]})
        self.assertNotIn("2", {pin["pin_no"] for pin in by_pkg["TFBGA289"]})
        self.assertNotIn("-", {pin["pin_no"] for pin in by_pkg["TFBGA289"]})

    def test_st_ball_definition_repairs_wrapped_numeric_pin_with_sequence_evidence(self):
        headers = ["Pin number LQFP64", "Pin name(function after reset)", "Pin type"]
        rows = [
            ["35", "PA1", "I/O"],
            ["3\n6", "PA2", "I/O"],
            ["37", "PA3", "I/O"],
        ]

        repaired = repair_st_table_rows(
            "Table 7. STM32Uxxx pin/ball definitions",
            headers,
            rows,
        )

        self.assertEqual([row[0] for row in repaired], ["35", "36", "37"])

    def test_st_ball_definition_keeps_ambiguous_wrapped_numeric_pin(self):
        headers = ["Pin number LQFP64", "Pin name(function after reset)", "Pin type"]
        rows = [
            ["2", "PA1", "I/O"],
            ["3\n6", "PA2", "I/O"],
            ["7", "PA3", "I/O"],
        ]

        repaired = repair_st_table_rows(
            "Table 7. STM32Uxxx pin/ball definitions",
            headers,
            rows,
        )

        self.assertEqual(repaired[1][0], "3\n6")

    def test_st_table_repair_promotes_package_label_row_to_headers(self):
        headers = (
            ["Pin number"] * 13
            + [
                "Pin name(function after reset)",
                "Pin type",
                "I/O structure",
                "Notes",
                "Alternate functions",
                "Additional functions",
            ]
        )
        package_row = [
            "LQFP64 SMPS",
            "LQFP100 SMPS",
            "UFBGA132 SMPS",
            "LQFP144 SMPS",
            "UFBGA150",
            "WLCSP150",
            "UFBGA169",
            "TFBGA208",
            "TFBGA216",
            "LQFP64",
            "LQFP100",
            "UFBGA132",
            "LQFP144",
            "TFBGA169",
            "Pin type",
            "I/O structure",
            "Notes",
            "Alternate functions",
            "Additional functions",
        ]
        rows = [
            package_row,
            [
                "1",
                "1",
                "A1",
                "1",
                "A1",
                "A1",
                "A1",
                "A1",
                "A1",
                "1",
                "1",
                "A1",
                "1",
                "A1",
                "PA0",
                "I/O",
                "FT",
                "-",
                "-",
                "-",
            ],
        ]

        repaired_headers, repaired_rows = repair_st_table(
            "Table 20. STM32U59xxx pin/ball definitions",
            headers,
            rows,
        )

        self.assertEqual(repaired_headers[13], "Pin number TFBGA169")
        self.assertTrue(repaired_headers[14].startswith("Pin name"))
        self.assertEqual(repaired_headers[15], "Pin type")
        self.assertEqual(repaired_rows[0][14], "PA0")

    def test_st_table_repair_splits_combined_package_pin_header(self):
        headers = [
            "Pin number LQFP48 SMPSUFQFPN48 SMPS",
            "Pin name(function after reset)",
            "Pin type",
        ]
        rows = [["1", "PA0", "I/O"], ["2", "PA1", "I/O"]]

        repaired_headers, repaired_rows = repair_st_table(
            "Table 9. STM32U535xx pin/ball definitions",
            headers,
            rows,
        )

        self.assertEqual(
            repaired_headers[:3],
            [
                "Pin number LQFP48 SMPS",
                "Pin number UFQFPN48 SMPS",
                "Pin name(function after reset)",
            ],
        )
        self.assertEqual(repaired_rows[0][:3], ["1", "1", "PA0"])

    def test_st_record_filter_drops_dash_pin_placeholders(self):
        self.assertFalse(should_keep_st_record({"pin_no": "-"}))
        self.assertFalse(should_keep_st_record({"pin_no": "--"}))
        self.assertTrue(should_keep_st_record({"pin_no": "A1"}))


if __name__ == "__main__":
    unittest.main()
