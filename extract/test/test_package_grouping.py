"""验证最终 pkg 使用物理封装名，并保持固定槽位数量。"""

import unittest
from unittest.mock import patch

import extract.pin_package_extractor as extractor
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.package_catalog_resolver import PackageAssignment
from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    TableDecision,
    derive_ball_side_package_assignment,
    extract_pin_package_info_from_table_candidates,
)


class PackageGroupingTest(unittest.TestCase):
    """覆盖单封装名称回退和表内多封装真实名称的通用行为。"""

    def test_ball_bottom_and_top_columns_split_output_symbols(self) -> None:
        """BALL BOTTOM/TOP 是同一物理封装的两个独立 symbol 输出空间。"""

        plan = MultiPackagePlan(
            True,
            "package_columns",
            (
                PackageBinding("BOTTOM", 0, 2, 3),
                PackageBinding("TOP", 1, 2, 3),
            ),
        )
        assignment = PackageAssignment("catalog:0", "a", "package_side_label")
        headers = ["BALL BOTTOM [1]", "BALL TOP [1]", "PIN NAME [2]", "TYPE [4]"]

        bottom = derive_ball_side_package_assignment(assignment, plan, 0, headers)
        top = derive_ball_side_package_assignment(assignment, plan, 1, headers)

        self.assertEqual(bottom.package_key, "catalog:0:ball_side:bottom")
        self.assertEqual(bottom.pkg, "a_bottom")
        self.assertEqual(top.package_key, "catalog:0:ball_side:top")
        self.assertEqual(top.pkg, "a_top")

    def test_bottom_top_words_without_ball_headers_do_not_split_symbols(self) -> None:
        """普通 bottom/top 文字不能影响多封装输出命名。"""

        plan = MultiPackagePlan(
            True,
            "package_columns",
            (
                PackageBinding("BOTTOM", 0, 2, 3),
                PackageBinding("TOP", 1, 2, 3),
            ),
        )
        assignment = PackageAssignment("catalog:0", "QFN", "matched")
        headers = ["BOTTOM PIN", "TOP PIN", "PIN NAME", "TYPE"]

        self.assertEqual(
            derive_ball_side_package_assignment(assignment, plan, 0, headers),
            assignment,
        )

    def test_single_package_without_catalog_uses_slot_fallback(self) -> None:
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

    def test_confirmed_package_count_is_kept_when_one_slot_has_no_rows(self) -> None:
        """名称关联失败不能删除已由总述表确认的物理封装槽位。"""

        summary = TableCandidate(
            html=(
                "<table>"
                "<tr><td>DEVICE</td><td>PACKAGE</td></tr>"
                "<tr><td>DEV-A</td><td>QFN</td></tr>"
                "<tr><td>DEV-B</td><td>BGA</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Device Information",
            group_context="Device Information",
        )
        pin_table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>PIN NAME</td><td>TYPE</td></tr>"
                "<tr><td>1</td><td>VDD</td><td>P</td></tr>"
                "</table>"
            ),
            page_idx=1,
            title="DEV-A Pin Functions",
            group_context="DEV-A Pin Functions",
        )
        columns = [
            ColumnDecision(0, "PIN NO.", "pin_no"),
            ColumnDecision(1, "PIN NAME", "pin_name"),
            ColumnDecision(2, "TYPE", "type"),
        ]

        def package_classifier(table, source_name, target_tables):
            if table.title != "Device Information":
                return {
                    "is_package_summary": False,
                    "table_role": "irrelevant",
                    "header_row_index": 0,
                    "columns": [],
                }
            return {
                "is_package_summary": True,
                "table_role": "identity_summary",
                "header_row_index": 0,
                "columns": [
                    {"column_index": 0, "role": "package_identity"},
                    {"column_index": 1, "role": "package_type"},
                ],
            }

        def package_batch_classifier(tables, source_name, target_tables):
            return {
                request_id: package_classifier(
                    table,
                    source_name,
                    target_tables,
                )
                for request_id, table in tables
            }

        with (
            patch.object(
                extractor,
                "decide_all_tables",
                return_value={1: TableDecision(True, columns=columns)},
            ),
            patch(
                "extract.semantic_classifier.classify_package_catalog_tables",
                side_effect=package_batch_classifier,
            ),
        ):
            result = extract_pin_package_info_from_table_candidates(
                [summary, pin_table],
                use_semantic_classifier=True,
            )

        self.assertEqual([item["pkg"] for item in result], ["QFN", "BGA"])
        self.assertEqual(len(result[0]["group_list"]), 1)
        self.assertEqual(result[1]["group_list"], [])


if __name__ == "__main__":
    unittest.main()
