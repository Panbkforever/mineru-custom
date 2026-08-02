"""特殊表直通保留与直接过滤的结构回归测试。"""

import unittest

from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
)
from extract.special_table_handlers import (
    find_special_table_match,
    register_word_pin_affected_table_filter,
)


class SpecialTableHandlersTest(unittest.TestCase):
    """验证特殊规则的严格命中条件和普通表保护边界。"""

    def test_word_bit_table_with_pin_affected_is_rejected(self) -> None:
        """PIN AFFECTED 是寄存器辅助列时，整表必须在模型前过滤。"""

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>BIT</td><td>BIT NAME</td><td></td>"
                "<td>DESCRIPTION / FUNCTION</td><td>TYPE</td>"
                "<td>POWER-UP CONDITION</td><td>PIN AFFECTED</td></tr>"
                "<tr><td>0</td><td>SEL</td><td></td>"
                "<td>Register selection</td><td>R/W</td>"
                "<td>0</td><td>F1, G1</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 1. Word 0",
        )

        result = extract_pin_package_info_from_table_candidates([table])

        self.assertEqual(result, [])

    def test_filter_returns_explicit_rejection(self) -> None:
        """特殊过滤结果必须明确表达拒绝，不能伪装成空列保留结果。"""

        match = register_word_pin_affected_table_filter(
            "Table 2. Word 1",
            [
                "BIT",
                "BIT NAME",
                "",
                "DESCRIPTION / FUNCTION",
                "TYPE",
                "POWER-UP CONDITION",
                "PIN AFFECTED",
            ],
            [["0", "MODE", "", "Mode select", "R/W", "0", "A1"]],
        )

        self.assertIsNotNone(match)
        self.assertFalse(match.should_extract)
        self.assertEqual(match.columns, ())

    def test_same_headers_without_word_title_do_not_match(self) -> None:
        """没有严格 Word 表题时不扩大过滤范围，继续走原模型链路。"""

        match = find_special_table_match(
            "Pin Functions",
            ["BIT", "BIT NAME", "DESCRIPTION / FUNCTION", "PIN AFFECTED"],
            [["0", "GPIO", "Configured by register", "A1"]],
        )

        self.assertIsNone(match)

    def test_direct_pin_number_axis_prevents_rejection(self) -> None:
        """存在真正 PIN NO. 主轴时，即使表题含 Word 也不能按辅助表过滤。"""

        match = register_word_pin_affected_table_filter(
            "Table 3. Word 2",
            [
                "PIN NO.",
                "BIT",
                "BIT NAME",
                "DESCRIPTION / FUNCTION",
                "PIN AFFECTED",
            ],
            [["A1", "0", "MODE", "Mode select", "B2"]],
        )

        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
