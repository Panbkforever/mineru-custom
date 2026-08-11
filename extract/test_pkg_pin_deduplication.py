import unittest

from extract.pin_package_extractor import deduplicate_pins_within_packages


class PackagePinDeduplicationTest(unittest.TestCase):
    def test_same_number_and_name_are_deduplicated_and_descriptions_are_merged(self) -> None:
        result = [
            {
                "pkg": "BGA",
                "group_list": [
                    {
                        "group": "Table 1",
                        "pin_list": [
                            {
                                "pin_no": "A1",
                                "pin_name": "VDD",
                                "type": "P",
                                "description": "Core power",
                            },
                            {"pin_no": "A1", "pin_name": "VDD", "type": "I"},
                        ],
                    },
                    {
                        "group": "Table 2",
                        "pin_list": [
                            {
                                "pin_no": "A1",
                                "pin_name": "VDD",
                                "type": "P",
                                "description": "Connect to 1.2 V",
                            },
                            {
                                "pin_no": "A1",
                                "pin_name": "VDD",
                                "type": "P",
                                "description": "Core power",
                            },
                        ],
                    },
                ],
            }
        ]

        deduplicated = deduplicate_pins_within_packages(result)

        self.assertEqual(len(deduplicated[0]["group_list"]), 1)
        pins = deduplicated[0]["group_list"][0]["pin_list"]
        self.assertEqual(len(pins), 1)
        self.assertEqual(
            pins[0]["description"],
            "Core power\nConnect to 1.2 V",
        )
        # type 不参与判重，重复记录保留第一次出现的 type。
        self.assertEqual(pins[0]["type"], "P")

    def test_identical_pins_in_different_packages_are_not_merged(self) -> None:
        result = [
            {
                "pkg": "BGA",
                "group_list": [
                    {
                        "group": "Pins",
                        "pin_list": [
                            {"pin_no": "1", "pin_name": "GND", "type": "G"}
                        ],
                    }
                ],
            },
            {
                "pkg": "QFN",
                "group_list": [
                    {
                        "group": "Pins",
                        "pin_list": [
                            {"pin_no": "1", "pin_name": "GND", "type": "G"}
                        ],
                    }
                ],
            },
        ]

        deduplicated = deduplicate_pins_within_packages(result)

        self.assertEqual(
            [len(package["group_list"][0]["pin_list"]) for package in deduplicated],
            [1, 1],
        )


if __name__ == "__main__":
    unittest.main()
