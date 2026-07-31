"""多封装候选召回与文档槽位绑定的通用回归测试。"""

import unittest
from unittest.mock import patch

import extract.pin_package_extractor as extractor
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.package_catalog_resolver import (
    PackageCatalogEntry,
    bind_multi_package_entries,
    deduplicate_redundant_catalog_entries,
)
from extract.pin_package_extractor import (
    ColumnDecision,
    TableCandidate,
    TableDecision,
    extract_pin_package_info_from_table_candidates,
)


class PackageBindingRegressionTest(unittest.TestCase):
    """验证多层表头和多封装分支不会在候选或绑定阶段丢失。"""

    def test_chinese_spanned_header_retries_before_candidate_rejection(self) -> None:
        """中文三级表头不能被 rough parser 提前拒绝。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><th colspan='5'>引脚</th><th rowspan='3'>类型</th>"
                "<th rowspan='3'>说明</th></tr>"
                "<tr><th rowspan='2'>名称</th><th colspan='2'>PWP</th>"
                "<th colspan='2'>RGE</th></tr>"
                "<tr><th>DRV8256</th><th>DRV8256E</th>"
                "<th>DRV8256</th><th>DRV8256E</th></tr>"
                "<tr><td>nSLEEP</td><td>1</td><td>2</td><td>3</td>"
                "<td>4</td><td>I</td><td>Sleep input</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="引脚功能",
            group_context="引脚功能",
        )
        columns = [
            ColumnDecision(0, "引脚 名称", "pin_name"),
            ColumnDecision(1, "引脚 PWP DRV8256", "pin_no"),
            ColumnDecision(2, "引脚 PWP DRV8256E", "pin_no"),
            ColumnDecision(3, "引脚 RGE DRV8256", "pin_no"),
            ColumnDecision(4, "引脚 RGE DRV8256E", "pin_no"),
            ColumnDecision(5, "类型", "type"),
        ]

        with patch.object(
            extractor,
            "decide_all_tables",
            return_value={0: TableDecision(True, columns=columns)},
        ):
            result = extract_pin_package_info_from_table_candidates([table])

        self.assertEqual(len(result), 4)
        self.assertEqual(
            [
                package["group_list"][0]["pin_list"][0]["pin_no"]
                for package in result
            ],
            ["1", "2", "3", "4"],
        )

    def test_pin_count_disambiguates_reversed_package_columns(self) -> None:
        """8/14 引脚分支必须按数量绑定，不能都落入第一个 TSSOP。"""

        entries = [
            PackageCatalogEntry("slot:0", package_type="TSSOP", package_drawing="PW", pin_count="14"),
            PackageCatalogEntry("slot:1", package_type="TSSOP", package_drawing="PW", pin_count="8"),
        ]

        bound = bind_multi_package_entries(entries, ["8-PIN PW", "14-PIN PW"])

        self.assertEqual([entry.package_key for entry in bound], ["slot:1", "slot:0"])

    def test_identity_footnote_and_catalog_order_do_not_merge_branches(self) -> None:
        """同为 WQFN 的 H/P/S 分支按型号绑定，并忽略型号末尾脚注。"""

        entries = [
            PackageCatalogEntry("slot:0", identity_name="DRV8311H", package_type="WQFN", pin_count="24"),
            PackageCatalogEntry("slot:1", identity_name="DRV8311S(2)", package_type="WQFN", pin_count="24"),
            PackageCatalogEntry("slot:2", identity_name="DRV8311P", package_type="WQFN", pin_count="24"),
        ]

        bound = bind_multi_package_entries(
            entries,
            ["DRV8311H", "DRV8311P", "DRV8311S"],
        )

        self.assertEqual(
            [entry.package_key for entry in bound],
            ["slot:0", "slot:2", "slot:1"],
        )

    def test_duplicate_physical_catalog_rows_do_not_create_extra_slot(self) -> None:
        """组合写法与分列写法表示同一物理封装时只保留一个槽位。"""

        entries = [
            PackageCatalogEntry("", package_type="(TSSOP-14) - PW"),
            PackageCatalogEntry("", package_type="TSSOP", package_drawing="PW", pin_count="14"),
            PackageCatalogEntry("", package_type="TSSOP", package_drawing="PW", pin_count="8"),
        ]

        deduplicated = deduplicate_redundant_catalog_entries(entries)

        self.assertEqual(len(deduplicated), 2)
        self.assertEqual(
            [(entry.package_drawing, entry.pin_count) for entry in deduplicated],
            [("PW", "14"), ("PW", "8")],
        )


if __name__ == "__main__":
    unittest.main()
