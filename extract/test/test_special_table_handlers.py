"""特殊表直通保留与直接过滤的结构回归测试。"""

import unittest

from extract.pin_package_extractor import (
    TableCandidate,
    extract_pin_package_info_from_table_candidates,
)
from extract.special_table_handlers import (
    find_special_table_match,
    mii_rmii_rgmii_pin_mux_table_filter,
    register_word_pin_affected_table_filter,
    supplemental_characteristics_table_filter,
    unused_pins_connection_table_filter,
)


class SpecialTableHandlersTest(unittest.TestCase):
    """验证特殊规则的严格命中条件和普通表保护边界。"""

    def test_unused_pins_connection_table_is_rejected(self) -> None:
        """Connections for Unused Pins 表必须在模型前直接过滤。"""

        match = unused_pins_connection_table_filter(
            "Table 7-3. Connections for Unused Pins – RGZ Package",
            ["Pin", "Signal", "Connection Requirements"],
            [["3", "NC", "Tie to ground"]],
        )

        self.assertIsNotNone(match)
        self.assertFalse(match.should_extract)
        self.assertEqual(match.handler_name, "unused_pins_connection_table_filter")

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>Pin</td><td>Signal</td><td>Connection Requirements</td></tr>"
                "<tr><td>3</td><td>NC</td><td>Tie to ground</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="Table 7-4. Connection for Unused Pins and Modules – RKP Package",
        )
        self.assertEqual(extract_pin_package_info_from_table_candidates([table]), [])

    def test_unused_word_without_connection_title_does_not_match(self) -> None:
        """普通 unused 说明标题不能被扩大过滤。"""

        match = find_special_table_match(
            "Table 7-1. Signal Descriptions – RGZ Package",
            ["Pin", "Signal", "Description"],
            [["3", "GPIO", "Unused by default"]],
        )

        self.assertIsNone(match)

    def test_mii_rmii_rgmii_pin_mux_table_is_rejected(self) -> None:
        """固定六列表头的以太网复用矩阵必须在模型前直接过滤。"""

        match = mii_rmii_rgmii_pin_mux_table_filter(
            "表 7-3 GMAC1 Pin 复用表",
            ["Pin No.", "MII MAC", "MII PHY", "RMII MAC", "RMII PHY", "RGMII"],
            [["56", "M1M_CRS", "-", "-", "-", "-"]],
        )

        self.assertIsNotNone(match)
        self.assertFalse(match.should_extract)
        self.assertEqual(match.handler_name, "mii_rmii_rgmii_pin_mux_table_filter")

        table = TableCandidate(
            html=(
                "<table>"
                "<tr><td>Pin No.</td><td>MII MAC</td><td>MII PHY</td>"
                "<td>RMII MAC</td><td>RMII PHY</td><td>RGMII</td></tr>"
                "<tr><td>56</td><td>M1M_CRS</td><td>-</td>"
                "<td>-</td><td>-</td><td>-</td></tr>"
                "</table>"
            ),
            page_idx=0,
            title="表 7-3 GMAC1 Pin 复用表",
        )
        self.assertEqual(extract_pin_package_info_from_table_candidates([table]), [])

    def test_partial_ethernet_mode_headers_do_not_match(self) -> None:
        """模式列不完整时不得扩大特殊过滤范围。"""

        match = find_special_table_match(
            "Pin Functions",
            ["Pin No.", "MII MAC", "MII PHY", "RGMII"],
            [["56", "M1M_CRS", "-", "-"]],
        )

        self.assertIsNone(match)

    def test_supplemental_characteristics_tables_are_rejected(self) -> None:
        """复用和电源补充说明表必须在模型前直接过滤。"""

        for title in (
            "Table 4-3. Multiplexing Characteristics",
            "Table 4-27. Power Supplies Description",
        ):
            with self.subTest(title=title):
                match = supplemental_characteristics_table_filter(
                    title,
                    ["Signal Name", "Description", "Ball (ZCN Pkg.)"],
                    [["VDD", "Power supply", "A1"]],
                )

                self.assertIsNotNone(match)
                self.assertFalse(match.should_extract)
                self.assertEqual(
                    match.handler_name,
                    "supplemental_characteristics_table_filter",
                )

    def test_signal_description_package_tables_do_not_match_supplemental_filter(
        self,
    ) -> None:
        """Signal Descriptions 可能是真正 pin 表，不能被新增规则误伤。"""

        match = find_special_table_match(
            "Table 4-1. Signal Descriptions – RGZ Package",
            ["Pin", "Signal Name", "Type"],
            [["1", "DIO_0", "I/O"]],
        )

        self.assertIsNone(match)

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
