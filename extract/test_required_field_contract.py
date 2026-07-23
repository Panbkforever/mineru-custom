"""验证物理引脚提取的字段契约，不绑定任何具体 PDF 样例。"""

import unittest

from extract.multi_package_extractor import BoundPackageRow
from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    decision_from_schema,
    extract_pin_package_info_from_table_candidates,
    extract_records_from_bound_package_row,
)


class RequiredFieldContractTest(unittest.TestCase):
    """覆盖表级字段映射和行级空值处理的通用分支。"""

    def test_semantic_result_without_pin_number_column_is_rejected(self) -> None:
        """模型只有名称和类型映射时，代码必须在行提取前明确拒绝。"""

        item = {"headers": ["Pin Name", "Type"]}
        decision = decision_from_schema(
            {
                "should_extract": True,
                "columns": [
                    {"column_index": 0, "field": "pin_name"},
                    {"column_index": 1, "field": "type"},
                ],
            },
            item,
        )

        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "semantic_missing_pin_no_column")

    def test_pin_number_only_table_fills_reserved_name(self) -> None:
        """只有编号列的普通表仍应输出，并把缺失名称补成 Reserved。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NUMBERS</td></tr>"
                "<tr><td>A1, A2</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Reserved Physical Pins",
        )

        result = extract_pin_package_info_from_table_candidates([table])
        pins = result[0]["group_list"][0]["pin_list"]

        self.assertEqual([pin["pin_no"] for pin in pins], ["A1", "A2"])
        self.assertEqual([pin["pin_name"] for pin in pins], ["Reserved", "Reserved"])

    def test_empty_pin_number_cell_is_preserved_after_mapping(self) -> None:
        """编号列已确定后，空编号数据行不得在拆分阶段消失。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>SIGNAL NAME</td><td>TYPE</td></tr>"
                "<tr><td></td><td>NC_SIGNAL</td><td>I</td></tr>"
                "<tr><td>A3</td><td>ACTIVE_SIGNAL</td><td>O</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Physical Pin List",
        )

        result = extract_pin_package_info_from_table_candidates([table])
        pins = result[0]["group_list"][0]["pin_list"]

        self.assertEqual([pin["pin_no"] for pin in pins], ["", "A3"])
        self.assertEqual(
            [pin["pin_name"] for pin in pins],
            ["NC_SIGNAL", "ACTIVE_SIGNAL"],
        )

    def test_fully_empty_layout_row_is_not_emitted(self) -> None:
        """完全空白的 HTML 排版行不能变成空编号 Reserved 记录。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>SIGNAL NAME</td></tr>"
                "<tr><td></td><td></td></tr>"
                "<tr><td>A1</td><td>ACTIVE_SIGNAL</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Physical Pin List",
        )

        result = extract_pin_package_info_from_table_candidates([table])
        pins = result[0]["group_list"][0]["pin_list"]

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["pin_no"], "A1")

    def test_multi_package_empty_pin_number_is_also_preserved(self) -> None:
        """多封装绑定分支与单封装分支遵守同一空编号规则。"""

        records = extract_records_from_bound_package_row(
            BoundPackageRow(
                package="PKG-A",
                row_index=0,
                pin_no="",
                pin_name="NC_SIGNAL",
                pin_type="I",
            )
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pin_no"], "")
        self.assertEqual(records[0]["pin_name"], "NC_SIGNAL")

    def test_complete_mapping_still_extracts_normally(self) -> None:
        """编号、名称和类型齐全的既有路径不能受到新校验影响。"""

        columns = [
            ColumnDecision(0, "PIN NO.", "pin_no"),
            ColumnDecision(1, "SIGNAL NAME", "pin_name"),
            ColumnDecision(2, "TYPE", "type"),
        ]
        decision = decision_from_schema(
            {
                "should_extract": True,
                "columns": [
                    {"column_index": column.index, "field": column.field_name}
                    for column in columns
                ],
            },
            {"headers": [column.raw_header for column in columns]},
        )

        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "semantic_selected")


if __name__ == "__main__":
    unittest.main()
