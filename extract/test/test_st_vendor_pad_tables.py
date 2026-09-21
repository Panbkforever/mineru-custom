import os
import unittest

from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
)


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


if __name__ == "__main__":
    unittest.main()
