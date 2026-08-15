"""多层表头名称结构与字段校验的通用测试。"""

import unittest

from extract.multi_package_extractor import (
    analyze_multi_package_table,
    iter_bound_package_rows,
)
from extract.pin_package_extractor import (
    ColumnDecision,
    choose_header_row,
    extract_records_from_row,
    reconcile_name_column_decisions,
)
from extract.table_header_structure import (
    analyze_name_column_layout,
    build_header_paths,
    extend_header_index_for_name_branches,
    parse_spanned_table,
    resolve_header_boundary,
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

    def test_repeated_pin_name_field_blocks_form_package_plan_without_pipe(
        self,
    ) -> None:
        headers = [
            "ALV BALL #",
            "ALV SIGNAL NAME",
            "ALX BALL #",
            "ALX SIGNAL NAME",
        ]
        data_rows = [
            ["A2", "DDR0_DQ1", "B2", "VSS"],
            ["A3", "DDR0_DQ0", "B3", "TDI"],
        ]
        decisions = [
            ColumnDecision(0, headers[0], "pin_no"),
            ColumnDecision(1, headers[1], "pin_name"),
            ColumnDecision(2, headers[2], "pin_no"),
            ColumnDecision(3, headers[3], "pin_name"),
        ]

        plan = analyze_multi_package_table(
            title="AM243x Package Comparison Table (ALV vs. ALX)",
            header_rows=[headers],
            headers=headers,
            data_rows=data_rows,
            columns=decisions,
        )

        self.assertTrue(plan.is_multi_package)
        self.assertEqual(plan.mode, "package_field_blocks")
        self.assertEqual(
            [
                (binding.package, binding.pin_no_column, binding.pin_name_column)
                for binding in plan.bindings
            ],
            [("ALV", 0, 1), ("ALX", 2, 3)],
        )
        self.assertEqual(
            [
                (row.package, row.pin_no, row.pin_name)
                for row in iter_bound_package_rows(plan, data_rows)
            ],
            [
                ("ALV", "A2", "DDR0_DQ1"),
                ("ALV", "A3", "DDR0_DQ0"),
                ("ALX", "B2", "VSS"),
                ("ALX", "B3", "TDI"),
            ],
        )

    def test_plain_row_extraction_does_not_pipe_multiple_pin_names(self) -> None:
        records = extract_records_from_row(
            ["A2", "DDR0_DQ1", "VSS"],
            [
                ColumnDecision(0, "BALL #", "pin_no"),
                ColumnDecision(1, "ALV SIGNAL NAME", "pin_name"),
                ColumnDecision(2, "ALX SIGNAL NAME", "pin_name"),
            ],
        )

        self.assertEqual(records[0]["pin_name"], "DDR0_DQ1")
        self.assertNotIn("|", records[0]["pin_name"])

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

    def test_name_role_footnotes_never_create_package_branches(self) -> None:
        """BALL/SIGNAL NAME 的脚注编号不能被当作 package 标签。"""

        rows = [
            ["BALL NAME [2]", "SIGNAL NAME [3]", "ZCZ_C NO.", "ZCZ_S NO."],
            ["PAD_A", "SIG_A", "A1", "B1"],
            ["", "SIG_B", "A2", "B2"],
        ]
        paths = build_header_paths(rows, 0)
        layout = analyze_name_column_layout(paths)

        self.assertEqual(layout.mode, "equivalent_names")

        # 等价名称列先选完整的一列，随后两个独立编号列仍应形成两个物理
        # package 分支；名称列等价与多封装编号列是两层互不冲突的结构。
        headers = [path.combined for path in paths]
        decisions = reconcile_name_column_decisions(
            [
                ColumnDecision(0, headers[0], "pin_name"),
                ColumnDecision(1, headers[1], "pin_name"),
                ColumnDecision(2, headers[2], "pin_no"),
                ColumnDecision(3, headers[3], "pin_no"),
            ],
            headers,
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

        plan = analyze_multi_package_table(
            title="Pin Attributes",
            header_rows=rows[:1],
            headers=headers,
            data_rows=rows[1:],
            columns=decisions,
            name_layout=layout,
        )
        self.assertTrue(plan.is_multi_package)
        self.assertEqual(plan.mode, "package_columns")
        self.assertEqual(
            [binding.pin_no_column for binding in plan.bindings],
            [2, 3],
        )

    def test_mode_and_pinlist_name_views_do_not_create_package_slots(self) -> None:
        """运行模式名称和 Pinlist 名称并排时只形成名称视图分支。"""

        rows = [
            ["SOP Mode Signal Name", "Pinlist Signal Name", "PIN NO."],
            ["BOOT", "GPIO0", "A1"],
        ]
        layout = analyze_name_column_layout(build_header_paths(rows, 0))

        self.assertEqual(layout.mode, "parallel_name_branches")
        self.assertEqual(
            [branch.label for branch in layout.branches],
            ["SOP Mode", "Pinlist"],
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

    def test_model_labels_below_repeated_parent_remain_in_header(self) -> None:
        """型号中的数字或连字符不能被误判成正式引脚数据。"""

        rows = [
            ["PIN", "PIN", "PIN", "TYPE", "DESCRIPTION"],
            ["NAME", "NO.", "NO.", "TYPE", "DESCRIPTION"],
            ["NAME", "DEVICE-A-Q1", "DEVICE-B-Q1", "TYPE", "DESCRIPTION"],
            ["SUPPLY", "14", "15", "P", "Analog supply"],
        ]

        boundary = resolve_header_boundary(rows, 1)
        self.assertEqual(boundary.header_end, 2)
        self.assertEqual(boundary.data_start, 3)

        header_index, headers = choose_header_row(rows, semantic=True)
        self.assertEqual(header_index, 2)
        self.assertEqual(headers[1], "PIN NO. DEVICE-A-Q1")
        self.assertEqual(headers[2], "PIN NO. DEVICE-B-Q1")

    def test_blank_parent_is_repaired_only_by_shared_child_structure(self) -> None:
        """父表头内部空位只由相邻列的共同子分组关系补齐。"""

        html = (
            "<table>"
            "<tr><th colspan='4'>PIN</th><th></th>"
            "<th rowspan='3'>TYPE</th><th rowspan='3'>DESCRIPTION</th></tr>"
            "<tr><th rowspan='2'>NAME</th><th colspan='2'>PKG-A</th>"
            "<th colspan='2'>PKG-B</th></tr>"
            "<tr><th>DEVICE-A</th><th>DEVICE-B</th>"
            "<th>DEVICE-A</th><th>DEVICE-B</th></tr>"
            "<tr><td>SUPPLY</td><td>1</td><td>2</td><td>3</td><td>4</td>"
            "<td>P</td><td>Analog supply</td></tr>"
            "</table>"
        )
        rows = parse_spanned_table(html)
        header_index, _ = choose_header_row(rows, semantic=True)
        self.assertEqual(header_index, 2)

        paths = build_header_paths(rows, header_index)
        self.assertEqual(paths[4].parts[0], "PIN")
        headers = [path.combined for path in paths]
        # 字段语义由模型/规则在结构冻结后返回；结构层只需确保四个分支列
        # 都拥有完整父路径，不能因第 5 列父节点曾为空而漏掉该分支。
        decisions = [
            ColumnDecision(0, headers[0], "pin_name"),
            *[
                ColumnDecision(index, headers[index], "pin_no")
                for index in range(1, 5)
            ],
            ColumnDecision(5, headers[5], "type"),
        ]
        plan = analyze_multi_package_table(
            title="",
            header_rows=rows[: header_index + 1],
            headers=headers,
            data_rows=rows[header_index + 1 :],
            columns=decisions,
        )
        self.assertTrue(plan.is_multi_package)
        self.assertEqual(
            [
                binding.pin_no_column
                for binding in plan.bindings
            ],
            [1, 2, 3, 4],
        )

    def test_first_body_row_does_not_extend_single_parent_group(self) -> None:
        """缺少稳定外部列时，不把普通两列数据误认成子表头。"""

        rows = [
            ["AXIS", "AXIS"],
            ["VALUE-A", "VALUE-B"],
            ["VALUE-C", "VALUE-D"],
        ]
        boundary = resolve_header_boundary(rows, 0)
        self.assertEqual(boundary.header_end, 0)
        self.assertEqual(boundary.data_start, 1)

    def test_multi_package_stage_never_recovers_header_from_body(self) -> None:
        """结构阶段漏掉子表头时，多封装阶段不能删除数据首行补猜。"""

        headers = ["PIN NAME", "PIN NO.", "PIN NO.", "TYPE"]
        data_rows = [
            ["PIN NAME", "PKG-A", "PKG-B", "TYPE"],
            ["SUPPLY", "1", "2", "P"],
        ]
        decisions = [
            ColumnDecision(0, headers[0], "pin_name"),
            ColumnDecision(1, headers[1], "pin_no"),
            ColumnDecision(2, headers[2], "pin_no"),
            ColumnDecision(3, headers[3], "type"),
        ]

        plan = analyze_multi_package_table(
            title="",
            header_rows=[headers],
            headers=headers,
            data_rows=data_rows,
            columns=decisions,
        )
        self.assertFalse(plan.is_multi_package)
        self.assertEqual(data_rows[0], ["PIN NAME", "PKG-A", "PKG-B", "TYPE"])


if __name__ == "__main__":
    unittest.main()
