from extract.pin_package_extractor import split_pin_numbers


def test_numeric_pin_range_expands_all_spacing_variants() -> None:
    """纯数字 pin_no 范围应支持常见横杠空格形态。"""

    expected = ["1", "2", "3", "4", "5"]
    assert split_pin_numbers("1-5") == expected
    assert split_pin_numbers("1 - 5") == expected
    assert split_pin_numbers("1 -5") == expected
    assert split_pin_numbers("1- 5") == expected


def test_numeric_pin_range_preserves_leading_zero_width() -> None:
    """纯数字范围端点带前导零时保留编号宽度。"""

    assert split_pin_numbers("01-03") == ["01", "02", "03"]


def test_numeric_pin_range_keeps_descending_or_too_large_range() -> None:
    """倒序或异常超大纯数字范围不展开。"""

    assert split_pin_numbers("5-1") == ["5-1"]
    assert split_pin_numbers("1-1002") == ["1-1002"]


def test_existing_prefixed_and_bracketed_ranges_are_unchanged() -> None:
    """新增纯数字范围不能影响既有字母前缀和方括号范围。"""

    assert split_pin_numbers("A1-A5") == ["A1", "A2", "A3", "A4", "A5"]
    assert split_pin_numbers("L[7:12]") == ["L7", "L8", "L9", "L10", "L11", "L12"]
    assert split_pin_numbers("A1-C3") == ["A1-C3"]


def test_number_first_letter_range_expands_only_same_numeric_axis() -> None:
    """同一数字轴范围展开，不推测跨轴或倒序范围。"""
    assert split_pin_numbers("1A-1C, 2A - 2C") == ["1A", "1B", "1C", "2A", "2B", "2C"]
    assert split_pin_numbers("1A-2C") == ["1A-2C"]
    assert split_pin_numbers("1C-1A") == ["1C-1A"]
    assert split_pin_numbers("01A-01C") == ["01A", "01B", "01C"]
    assert split_pin_numbers("1H-1K") == ["1H-1K"]
