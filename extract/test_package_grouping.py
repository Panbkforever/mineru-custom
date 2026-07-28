"""验证最终 pkg 使用封装目录中的真实名称。"""

import unittest
from unittest.mock import patch

import extract.pin_package_extractor as extractor
from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    TableDecision,
    extract_pin_package_info_from_table_candidates,
)


class PackageGroupingTest(unittest.TestCase):
    """覆盖单封装未解析和表内多封装真实名称的通用行为。"""

    def test_single_package_without_catalog_keeps_empty_name(self) -> None:
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

        self.assertEqual([item["pkg"] for item in result], [""])
        self.assertEqual(
            result[0]["group_list"][0]["pin_list"][0]["pin_name"],
            "VDD",
        )

    def test_multi_package_columns_keep_real_labels(self) -> None:
        """结构已确认的封装专属编号列表头可补充真实目录名称。"""

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

        self.assertEqual([item["pkg"] for item in result], ["SSOP 28", "QFN 32"])
        self.assertEqual(
            [pin["pin_no"] for pin in result[0]["group_list"][0]["pin_list"]],
            ["1", "2"],
        )
        self.assertEqual(
            [pin["pin_no"] for pin in result[1]["group_list"][0]["pin_list"]],
            ["A1", "A2"],
        )


if __name__ == "__main__":
    unittest.main()
