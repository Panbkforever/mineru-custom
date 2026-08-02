"""寄存器引脚引用表初筛的通用回归测试。"""

import unittest

from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
)
from extract.register_reference_table_filter import is_register_pin_reference_table


class RegisterReferenceTableFilterTest(unittest.TestCase):
    """覆盖应过滤和必须保留的结构边界，不绑定具体 PDF 文件名。"""

    def test_register_table_with_relevant_pins_is_filtered(self) -> None:
        """寄存器字段是主轴、Relevant Pins 是辅助列时必须在模型前排除。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>Bit</td><td>Field</td><td>Access</td>"
                "<td>Default</td><td>Relevant Pins</td></tr>"
                "<tr><td>7:4</td><td>MODE</td><td>R/W</td>"
                "<td>0</td><td>A1, B2</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 2. Word 1",
        )

        result = extract_pin_package_info_from_table_candidates([table])

        self.assertEqual(result, [])

    def test_direct_pin_axis_prevents_register_filter(self) -> None:
        """真正的 Pin No. 轴优先，不能因同时出现 register 字段而误过滤。"""

        headers = ["Pin No.", "Signal Name", "Register Field", "Relevant Pins"]

        self.assertFalse(
            is_register_pin_reference_table("Pin Control", headers)
        )

    def test_description_mention_does_not_trigger_filter(self) -> None:
        """普通引脚表的 Description 正文提到寄存器时仍应正常提取。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>PIN NO.</td><td>SIGNAL NAME</td><td>TYPE</td>"
                "<td>DESCRIPTION</td></tr>"
                "<tr><td>A1</td><td>GPIO0</td><td>I/O</td>"
                "<td>Configured by register 27 bit 3.</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Pin Functions",
        )

        result = extract_pin_package_info_from_table_candidates([table])
        pins = result[0]["group_list"][0]["pin_list"]

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["pin_no"], "A1")
        self.assertEqual(pins[0]["pin_name"], "GPIO0")

    def test_auxiliary_pin_column_without_register_schema_is_not_filtered(self) -> None:
        """仅有 Associated Pins 不足以过滤，避免扩大初筛边界。"""

        headers = ["Function", "Associated Pins", "Connection Requirement"]

        self.assertFalse(
            is_register_pin_reference_table("Connectivity Requirements", headers)
        )


if __name__ == "__main__":
    unittest.main()
