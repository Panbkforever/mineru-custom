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


if __name__ == "__main__":
    unittest.main()
