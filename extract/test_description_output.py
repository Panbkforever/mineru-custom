import unittest

from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    append_description_column_decision,
    choose_header_row,
    extract_pin_package_info_from_table_candidates,
    read_optional_mapped_field,
)
from extract.multi_package_extractor import detect_package_selector_column


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

    def test_first_description_value_cannot_extend_header_boundary(self) -> None:
        rows = [
            ["PIN", "PIN", "DESCRIPTION"],
            ["NAME", "NO.", "DESCRIPTION"],
            [
                "Exposed Pad",
                "Pad",
                "Exposed pad of a generic TSSOP package",
            ],
            ["OUT", "1", "Output feedback"],
        ]

        header_index, headers = choose_header_row(rows)

        self.assertEqual(header_index, 1)
        self.assertEqual(headers, ["PIN NAME", "PIN NO.", "DESCRIPTION"])

    def test_description_header_cannot_be_package_selector(self) -> None:
        headers = [
            "PIN NAME Exposed Pad",
            "PIN NO. Pad",
            "DESCRIPTION Exposed pad of a generic TSSOP package",
        ]
        columns = [
            ColumnDecision(0, headers[0], "pin_name", 5),
            ColumnDecision(1, headers[1], "pin_no", 5),
        ]

        plan = detect_package_selector_column(
            headers=headers,
            data_rows=[
                ["OUT", "1", "Output feedback for the package"],
                ["GATE", "2", "Gate drive output for the package"],
            ],
            columns=columns,
        )

        self.assertIsNone(plan)

    def test_description_package_word_does_not_create_multiple_packages(self) -> None:
        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN</td><td>PIN</td><td>DESCRIPTION</td></tr>"
                "<tr><td>NAME</td><td>NO.</td><td>DESCRIPTION</td></tr>"
                "<tr><td>Exposed Pad</td><td>Pad</td>"
                "<td>Exposed pad of a generic TSSOP package</td></tr>"
                "<tr><td>OUT</td><td>1</td>"
                "<td>Output feedback for the package</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Pin Functions",
            group_context="Pin Functions",
        )

        result = extract_pin_package_info_from_table_candidates([table])

        self.assertEqual(len(result), 1)
        # description 中出现 package 不能创造封装名称；没有目录证据时保持空。
        self.assertEqual(result[0]["pkg"], "a")
        pins = result[0]["group_list"][0]["pin_list"]
        self.assertEqual(
            [(pin["pin_no"], pin["pin_name"]) for pin in pins],
            [("Pad", "Exposed Pad"), ("1", "OUT")],
        )


if __name__ == "__main__":
    unittest.main()
