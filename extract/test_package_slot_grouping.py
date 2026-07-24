"""验证 pkg 只表示结构分组序号，不再承载真实封装名称。"""

import unittest
from unittest.mock import patch

import extract.pin_package_extractor as extractor
from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    TableDecision,
    build_public_result,
    extract_pin_package_info_from_table_candidates,
    get_package_slot_bucket,
    get_or_create_group,
    package_slot_label,
)


class PackageSlotGroupingTest(unittest.TestCase):
    """覆盖单封装、多封装和最终字母编号的通用行为。"""

    def test_package_slot_labels_are_stable(self) -> None:
        self.assertEqual(package_slot_label(0), "a")
        self.assertEqual(package_slot_label(1), "b")
        self.assertEqual(package_slot_label(25), "z")
        self.assertEqual(package_slot_label(26), "aa")
        self.assertEqual(package_slot_label(27), "ab")

    def test_single_package_table_always_uses_a(self) -> None:
        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>SIGNAL NAME</td><td>TYPE</td></tr>"
                "<tr><td>1</td><td>VDD</td><td>Power</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 1. Any Package Name",
            group_context="Table 1. Any Package Name",
        )

        result = extract_pin_package_info_from_table_candidates([table])

        self.assertEqual([item["pkg"] for item in result], ["a"])
        self.assertEqual(
            result[0]["group_list"][0]["pin_list"][0]["pin_name"],
            "VDD",
        )

    def test_public_result_uses_slots_not_package_text(self) -> None:
        packages = {}
        second = get_package_slot_bucket(packages, 1)
        first = get_package_slot_bucket(packages, 0)
        get_or_create_group(second, "Table 2").pin_list.append(
            {"pin_no": "B1", "pin_name": "SIG_B"}
        )
        get_or_create_group(first, "Table 1").pin_list.append(
            {"pin_no": "A1", "pin_name": "SIG_A"}
        )

        result = build_public_result(packages, include_debug=True)

        self.assertEqual([item["pkg"] for item in result], ["a", "b"])
        self.assertEqual([item["package_slot"] for item in result], [0, 1])
        self.assertNotIn("Package", "".join(item["pkg"] for item in result))

    def test_multi_package_columns_become_a_and_b_by_position(self) -> None:
        """真实表头文字只定位列，最终 pkg 必须按列序号输出。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NAME</td><td>SSOP 28 PIN</td>"
                "<td>QFN 32 PIN</td><td>TYPE</td></tr>"
                "<tr><td>VDD</td><td>1</td><td>A1</td><td>P</td></tr>"
                "<tr><td>GND</td><td>2</td><td>A2</td><td>P</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 1. Package Pins",
            group_context="Table 1. Package Pins",
        )
        columns = [
            ColumnDecision(0, "PIN NAME", "pin_name"),
            ColumnDecision(1, "SSOP 28 PIN", "pin_no"),
            ColumnDecision(2, "QFN 32 PIN", "pin_no"),
            ColumnDecision(3, "TYPE", "type"),
        ]

        with patch.object(
            extractor,
            "decide_all_tables",
            return_value={0: TableDecision(True, columns=columns)},
        ):
            result = extract_pin_package_info_from_table_candidates([table])

        self.assertEqual([item["pkg"] for item in result], ["a", "b"])
        self.assertEqual(
            [pin["pin_no"] for pin in result[0]["group_list"][0]["pin_list"]],
            ["1", "2"],
        )
        self.assertEqual(
            [pin["pin_no"] for pin in result[1]["group_list"][0]["pin_list"]],
            ["A1", "A2"],
        )
        self.assertNotIn("SSOP", str(result))
        self.assertNotIn("QFN", str(result))


if __name__ == "__main__":
    unittest.main()
