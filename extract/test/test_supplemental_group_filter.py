"""主引脚表存在时过滤补充说明表的回归测试。"""

from __future__ import annotations

from extract.pin_package_extractor import (
    filter_supplemental_signal_groups_when_primary_tables_exist,
)


def pin(pin_no: str = "1", pin_name: str = "A") -> dict[str, str]:
    return {"pin_no": pin_no, "pin_name": pin_name}


def test_signal_description_groups_are_removed_when_ball_characteristics_exist():
    result = [
        {
            "pkg": "FCBGA1",
            "group_list": [
                {
                    "group": "Table 2-2. Ball Characteristics (CBC Pkg.)(5)",
                    "pin_list": [pin("A1", "vss")],
                },
                {
                    "group": "Table 2-8. Video Interfaces – DSS Signals Description",
                    "pin_list": [pin("A2", "dss_data0")],
                },
            ],
        },
        {
            "pkg": "FCBGA2",
            "group_list": [
                {
                    "group": "Table 2-28. CBP Package Feed-Through Balls",
                    "pin_list": [pin("B1", "Feed-Through Pins")],
                },
                {
                    "group": "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
                    "pin_list": [pin("B2", "vdd")],
                },
            ],
        },
    ]

    filtered = filter_supplemental_signal_groups_when_primary_tables_exist(result)

    assert [
        group["group"]
        for package in filtered
        for group in package["group_list"]
    ] == [
        "Table 2-2. Ball Characteristics (CBC Pkg.)(5)",
        "Table 2-1. Ball Characteristics (CBP Pkg.)(3)",
    ]


def test_signal_description_groups_remain_when_no_primary_table_exists():
    result = [
        {
            "pkg": "a",
            "group_list": [
                {
                    "group": "Table 2-8. Video Interfaces – DSS Signals Description",
                    "pin_list": [pin("A2", "dss_data0")],
                }
            ],
        }
    ]

    filtered = filter_supplemental_signal_groups_when_primary_tables_exist(result)

    assert filtered[0]["group_list"][0]["group"] == (
        "Table 2-8. Video Interfaces – DSS Signals Description"
    )


def test_pin_functions_also_enable_supplemental_group_filtering():
    result = [
        {
            "pkg": "QFN",
            "group_list": [
                {
                    "group": "Pin Functions—32-Pin Device",
                    "pin_list": [pin("1", "IN")],
                },
                {
                    "group": "System and Miscellaneous Signals Description",
                    "pin_list": [pin("2", "GPIO")],
                },
            ],
        }
    ]

    filtered = filter_supplemental_signal_groups_when_primary_tables_exist(result)

    assert [group["group"] for group in filtered[0]["group_list"]] == [
        "Pin Functions—32-Pin Device"
    ]
