import unittest

from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    append_description_column_decision,
    extract_pin_package_info_from_table_candidates,
    read_optional_mapped_field,
)


class DescriptionOutputTest(unittest.TestCase):
    def test_single_package_records_keep_description_from_same_row(self) -> None:
        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>SIGNAL NAME</td><td>TYPE</td>"
                "<td>DESCRIPTION</td></tr>"
                "<tr><td>A1, A2</td><td>SIG_A, SIG_B</td><td>I/O</td>"
                "<td>Shared row description</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 1. Pin List",
            group_context="Table 1. Pin List",
        )

        result = extract_pin_package_info_from_table_candidates([table])

        pins = result[0]["group_list"][0]["pin_list"]
        self.assertEqual([pin["pin_no"] for pin in pins], ["A1", "A2"])
        self.assertEqual(
            [pin["description"] for pin in pins],
            ["Shared row description", "Shared row description"],
        )

    def test_description_is_absent_when_table_has_no_description_column(self) -> None:
        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>SIGNAL NAME</td><td>TYPE</td></tr>"
                "<tr><td>1</td><td>VDD</td><td>Power</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 2. Pin List",
            group_context="Table 2. Pin List",
        )

        result = extract_pin_package_info_from_table_candidates([table])

        pin = result[0]["group_list"][0]["pin_list"][0]
        self.assertNotIn("description", pin)

    def test_optional_reader_distinguishes_missing_and_empty_description(self) -> None:
        headers = ["PIN NO.", "SIGNAL NAME", "DESCRIPTION"]
        columns = append_description_column_decision(
            [
                ColumnDecision(0, headers[0], "pin_no"),
                ColumnDecision(1, headers[1], "pin_name"),
            ],
            headers,
        )

        self.assertEqual(read_optional_mapped_field(["1", "VDD", ""], columns, "description"), "")
        self.assertIsNone(
            read_optional_mapped_field(
                ["1", "VDD"],
                columns[:2],
                "description",
            )
        )


if __name__ == "__main__":
    unittest.main()
