"""多层表头名称结构与字段校验的通用测试。"""

import unittest

from extract.multi_package_extractor import (
    analyze_multi_package_table,
    iter_bound_package_rows,
)
from extract.pin_package_extractor import (
    ColumnDecision,
    choose_header_row,
    reconcile_name_column_decisions,
)
from extract.table_header_structure import (
    analyze_name_column_layout,
    build_header_paths,
    extend_header_index_for_name_branches,
    parse_spanned_table,
)


class TableHeaderStructureTest(unittest.TestCase):
    """验证普通等价名称列与分支名称列不会互相误判。"""

    def test_shared_number_and_branch_name_columns_form_multi_package_plan(
        self,
    ) -> None:
        html = (
            "<table>"
            "<tr><th colspan='3'>PIN</th><th rowspan='3'>No.</th>"
            "<th rowspan='3'>Type</th></tr>"
            "<tr><th colspan='3'>NAME</th></tr>"
            "<tr><th>Device A</th><th>Device B</th><th>Device C</th></tr>"
            "<tr><td>AIN0</td><td>BIN0</td><td>CIN0</td>"
            "<td>1</td><td>I</td></tr>"
            "<tr><td>AIN1</td><td>BIN1</td><td>CIN1</td>"
            "<td>2</td><td>O</td></tr>"
            "</table>"
        )
        rows = parse_spanned_table(html)
        header_index, _ = choose_header_row(rows, semantic=True)
        header_index = extend_header_index_for_name_branches(
            rows,
            header_index,
        )
        self.assertEqual(header_index, 2)

        paths = build_header_paths(rows, header_index)
        layout = analyze_name_column_layout(paths)
        self.assertEqual(layout.mode, "package_branches")
        self.assertEqual(
            [branch.label for branch in layout.branches],
            ["Device A", "Device B", "Device C"],
        )

        # 模型只返回一个名称列时，确定性结构必须恢复其余两个分支。
        headers = [path.combined for path in paths]
        data_rows = rows[header_index + 1 :]
        decisions = reconcile_name_column_decisions(
            [
                ColumnDecision(0, headers[0], "pin_name"),
                ColumnDecision(3, headers[3], "pin_no"),
                ColumnDecision(4, headers[4], "type"),
            ],
            headers,
            data_rows,
            layout,
        )
        self.assertEqual(
            [
                decision.index
                for decision in decisions
                if decision.field_name == "pin_name"
            ],
            [0, 1, 2],
        )

        plan = analyze_multi_package_table(
            title="Pin Functions",
            header_rows=rows[: header_index + 1],
            headers=headers,
            data_rows=data_rows,
            columns=decisions,
            name_layout=layout,
        )
        self.assertTrue(plan.is_multi_package)
        self.assertEqual(plan.mode, "package_name_columns")
        self.assertEqual(
            [(binding.pin_no_column, binding.pin_name_column) for binding in plan.bindings],
            [(3, 0), (3, 1), (3, 2)],
        )
        self.assertEqual(
            [
                (row.package, row.pin_no, row.pin_name)
                for row in iter_bound_package_rows(plan, data_rows)
            ],
            [
                ("Device A", "1", "AIN0"),
                ("Device A", "2", "AIN1"),
                ("Device B", "1", "BIN0"),
                ("Device B", "2", "BIN1"),
                ("Device C", "1", "CIN0"),
                ("Device C", "2", "CIN1"),
            ],
        )

    def test_ball_name_and_signal_name_choose_one_complete_column(self) -> None:
        rows = [
            ["BALL NAME", "SIGNAL NAME", "BALL NO.", "TYPE"],
            ["PAD_A", "SIG_A", "A1", "I"],
            ["", "SIG_B", "A2", "O"],
        ]
        paths = build_header_paths(rows, 0)
        layout = analyze_name_column_layout(paths)
        self.assertEqual(layout.mode, "equivalent_names")

        decisions = reconcile_name_column_decisions(
            [
                ColumnDecision(0, "BALL NAME", "pin_name"),
                ColumnDecision(1, "SIGNAL NAME", "pin_name"),
                ColumnDecision(2, "BALL NO.", "pin_no"),
                ColumnDecision(3, "TYPE", "type"),
            ],
            [path.combined for path in paths],
            rows[1:],
            layout,
        )
        self.assertEqual(
            [
                decision.index
                for decision in decisions
                if decision.field_name == "pin_name"
            ],
            [1],
        )

    def test_different_name_parents_do_not_consume_first_data_row(self) -> None:
        rows = [
            ["BALL NAME", "SIGNAL NAME", "BALL NO."],
            ["PAD_A", "SIG_A", "A1"],
        ]
        self.assertEqual(
            extend_header_index_for_name_branches(rows, 0),
            0,
        )

    def test_branch_parent_above_bare_name_is_supported(self) -> None:
        rows = [
            ["Package A", "Package B", "Pin No."],
            ["NAME", "NAME", "Pin No."],
            ["AIN", "BIN", "1"],
        ]
        paths = build_header_paths(rows, 1)
        layout = analyze_name_column_layout(paths)

        self.assertEqual(layout.mode, "package_branches")
        self.assertEqual(
            [branch.label for branch in layout.branches],
            ["Package A", "Package B"],
        )

    def test_role_and_branch_in_same_header_cell_are_separated(self) -> None:
        rows = [
            ["PIN NAME Package A", "PIN NAME Package B", "PIN NO."],
            ["AIN", "BIN", "1"],
        ]
        layout = analyze_name_column_layout(build_header_paths(rows, 0))

        self.assertEqual(layout.mode, "package_branches")
        self.assertEqual(
            [branch.label for branch in layout.branches],
            ["Package A", "Package B"],
        )


if __name__ == "__main__":
    unittest.main()
