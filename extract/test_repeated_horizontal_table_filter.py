"""横向重复引脚字段块过滤器的通用结构测试。"""

from extract.repeated_horizontal_table_filter import (
    is_repeated_horizontal_pin_block_table,
)


def test_filters_repeated_blocks_with_bare_number_headers():
    headers = [
        "No.",
        "Pin Name",
        "Type",
        "No.",
        "Pin Name",
        "Type",
        "No.",
        "Pin Name",
        "Type",
    ]

    assert is_repeated_horizontal_pin_block_table(headers)


def test_does_not_filter_single_block_with_bare_number_header():
    headers = ["No.", "Pin Name", "Type"]

    assert not is_repeated_horizontal_pin_block_table(headers)


def test_does_not_filter_multi_package_columns_or_extra_fields():
    headers = [
        "Pin Name",
        "SSOP 28 Pin",
        "QFN 28 Pin",
        "LQFP 48 Pin",
        "I/O Type",
        "Description",
    ]

    assert not is_repeated_horizontal_pin_block_table(headers)


def test_does_not_filter_repeated_blocks_with_description_column():
    headers = [
        "No.",
        "Pin Name",
        "Type",
        "Description",
        "No.",
        "Pin Name",
        "Type",
    ]

    assert not is_repeated_horizontal_pin_block_table(headers)
