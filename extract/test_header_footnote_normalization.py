"""字段表头纯数字脚注清洗的通用回归测试。"""

import unittest

from extract.pin_package_extractor import (
    choose_header_row,
    classify_header,
    strip_numeric_header_footnotes,
)


class HeaderFootnoteNormalizationTest(unittest.TestCase):
    """保证字段脚注不会阻断候选表召回，也不会扩大清洗范围。"""

    def test_multilevel_pin_headers_with_numeric_footnotes_are_detected(self) -> None:
        rows = [
            ["PIN (1)", "PIN (1)", "PIN (1)", "I/O(2)", "DESCRIPTION"],
            ["NAME", "PACKAGE_A", "PACKAGE_B", "I/O(2)", "DESCRIPTION"],
            ["DATA_0", "A1", "B1", "I", "Data input"],
        ]

        header_index, headers = choose_header_row(rows, semantic=True)

        self.assertEqual(header_index, 1)
        self.assertEqual(classify_header(headers[0])[0], "pin_name")
        self.assertEqual(classify_header(headers[3])[0], "type")

    def test_existing_headers_without_footnotes_remain_detectable(self) -> None:
        self.assertEqual(classify_header("PIN NAME"), ("pin_name", 5))
        self.assertEqual(classify_header("I/O"), ("type", 3))

    def test_only_pure_numeric_parentheses_are_removed(self) -> None:
        self.assertEqual(
            strip_numeric_header_footnotes("PIN (1) (2) NAME"),
            "PIN     NAME",
        )
        self.assertEqual(
            strip_numeric_header_footnotes("MODE (RGB) NAME"),
            "MODE (RGB) NAME",
        )


if __name__ == "__main__":
    unittest.main()
