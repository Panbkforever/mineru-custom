"""厂家分支不能额外过滤编号或名称；仅用合成表验证公共流程，不调用模型。"""

from html import escape
from unittest.mock import patch

import pytest

from extract import pin_package_extractor as extractor
from extract.vendors import get_vendor_profile


@pytest.mark.parametrize("vendor", ["ST", "ABOV", "GIGA"])
def test_no_extra_record_filter_is_registered(vendor):
    profile = get_vendor_profile(vendor)
    shared = get_vendor_profile("TI")
    assert profile.record_filterer is None
    for number in ["", "-", "--", "—", "NA", "N/A", "N.A.", "NOTAPPLICABLE", "Multiple", "A1"]:
        for name in ["", "NC", "N.C.", "EPAD", "Pad", "SIGNAL"]:
            record = {"pin_no": number, "pin_name": name}
            assert profile.should_keep_record(record) == shared.should_keep_record(record)


def table(rows):
    data = [["Pin number", "Pin name", "Type", "Description"], *rows]
    html = "<table>" + "".join("<tr>" + "".join(
        "<td>" + escape(cell) + "</td>" for cell in row
    ) + "</tr>" for row in data) + "</table>"
    return extractor.TableCandidate(html=html, page_idx=0, title="Pin descriptions")


def extract(vendor, rows):
    # 将模型入口设为异常，确保本测试只验证现有确定性提取流程。
    with patch.dict("os.environ", {"EXTRACT_VENDOR": vendor}), patch(
        "extract.semantic_classifier.classify_table_schema_batch", side_effect=AssertionError("禁止调用模型"),
    ), patch("extract.semantic_classifier.classify_package_catalog_tables", side_effect=AssertionError("禁止调用模型")):
        result = extractor.extract_pin_package_info_from_table_candidates(
            [table(rows)], use_semantic_classifier=False,
        )
    return [pin for package in result for group in package["group_list"] for pin in group["pin_list"]]


@pytest.mark.parametrize("vendor", ["ST", "ABOV", "GIGA"])
def test_selected_table_keeps_same_source_records_as_shared_flow(vendor):
    rows = [["1", "SIGNAL", "I", "Signal"], ["2", "VDD", "P", "Supply"],
            ["-", "EPAD", "P", "Exposed pad"], ["", "UNKNOWN", "I", "No number"],
            ["N/A", "NA_NAME", "I", "Source value"], ["NA", "NA_PLAIN", "I", "Source value"],
            ["N.A.", "NA_DOTS", "I", "Source value"],
            ["NOTAPPLICABLE", "NO_APPLICABLE", "I", "Source value"],
            ["Multiple", "NC", "", "No Connection"], ["", "", "", ""]]
    records = extract(vendor, rows)
    baseline = extract("TI", rows)
    key = lambda record: (record.get("pin_no", ""), record.get("pin_name", ""))
    assert [key(record) for record in records] == [key(record) for record in baseline]
    assert len(records) == len(rows) - 1
    assert any(record["pin_no"] == "-" and record["pin_name"] == "EPAD" for record in records)
    assert any(record["pin_no"] == "" and record["pin_name"] == "UNKNOWN" for record in records)
