"""并行 pin_no/pin_name 单元格拆分的通用结构测试。"""

from extract.parallel_cell_splitter import split_parallel_pin_names


def _split_test_pin_numbers(value: str) -> list[str]:
    """测试用编号拆分器；生产代码会传入项目统一的 split_pin_numbers。"""

    return [part.strip() for part in value.split(",") if part.strip()]


def test_descending_embedded_ranges_follow_br_groups() -> None:
    """降序范围应在每个换行分组内展开，并按原顺序形成一一对应。"""

    pin_no = (
        "A1, D3, E3, F3, G3\n"
        "C5, C4, C3, B2, A2\n"
        "G18, G17, G16, F18, F17\n"
        "J18, J17, H18, H17, H16"
    )
    pin_name = "LED[4:0]_3\nLED[4:0]_2\nLED[4:0]_1\nLED[4:0]*0"

    result = split_parallel_pin_names(
        pin_name,
        pin_no,
        20,
        split_pin_numbers=_split_test_pin_numbers,
    )

    assert result == [
        "LED4_3", "LED3_3", "LED2_3", "LED1_3", "LED0_3",
        "LED4_2", "LED3_2", "LED2_2", "LED1_2", "LED0_2",
        "LED4_1", "LED3_1", "LED2_1", "LED1_1", "LED0_1",
        "LED4*0", "LED3*0", "LED2*0", "LED1*0", "LED0*0",
    ]


def test_ascending_embedded_range_is_supported() -> None:
    """相同逻辑也必须支持升序范围，不能只针对一个示例的降序格式。"""

    result = split_parallel_pin_names(
        "GPIO[0:2]_A",
        "A1,A2,A3",
        3,
        split_pin_numbers=_split_test_pin_numbers,
    )

    assert result == ["GPIO0_A", "GPIO1_A", "GPIO2_A"]


def test_group_count_mismatch_does_not_guess() -> None:
    """任一组展开数量不同都不能广播或循环使用名称。"""

    result = split_parallel_pin_names(
        "LED[1:0]_A\nLED[1:0]_B",
        "A1,A2\nB1,B2,B3",
        5,
        split_pin_numbers=_split_test_pin_numbers,
    )

    assert result == []


def test_multiple_name_ranges_are_not_partially_expanded() -> None:
    """同一名称存在多个范围时关系不唯一，不能只展开第一个范围。"""

    result = split_parallel_pin_names(
        "BUS[1:0]_LANE[1:0]",
        "A1,A2,A3,A4",
        4,
        split_pin_numbers=_split_test_pin_numbers,
    )

    assert result == []


def test_existing_one_to_one_br_pairing_is_unchanged() -> None:
    """原有的普通换行一一对应分支必须保持不变。"""

    result = split_parallel_pin_names(
        "MDINT_0\nMDINT_1",
        "P18\nN16",
        2,
        split_pin_numbers=_split_test_pin_numbers,
    )

    assert result == ["MDINT_0", "MDINT_1"]
