"""模型批量调用协议的通用回归测试。"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from extract.package_catalog_resolver import (
    PackageCatalogTable,
    classify_package_catalog_candidates,
)
from extract.pin_package_extractor import (
    build_semantic_table_sample,
    decide_all_tables,
)
from extract.semantic_classifier import call_model_json, classify_table_schema_batch
from extract.table_header_structure import NameColumnLayout


class SemanticBatchingTests(unittest.TestCase):
    def test_model_json_default_timeout_is_ninety_seconds(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"choices":[{"message":{"content":"{\\"ok\\": true}"}}]}'
                )

        def fake_urlopen(request, timeout):
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.dict(os.environ, {}, clear=True), patch(
            "extract.semantic_classifier.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = call_model_json(
                {"task": "test"},
                api_key="test",
                system_prompt="Return JSON.",
                max_tokens=10,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["timeout"], 90.0)

    def test_small_pin_table_sends_all_data_rows(self):
        rows = [["PARENT"], ["PIN"]] + [[str(index)] for index in range(30)]

        sampled, metadata = build_semantic_table_sample(rows, header_index=1)

        self.assertEqual(sampled, rows)
        self.assertEqual(metadata["strategy"], "full")
        self.assertEqual(metadata["sampled_data_rows"], 30)

    def test_medium_pin_table_keeps_headers_and_head_middle_tail_rows(self):
        rows = [["PARENT"], ["PIN"]] + [[str(index)] for index in range(100)]

        sampled, metadata = build_semantic_table_sample(rows, header_index=1)

        self.assertEqual(sampled[:2], rows[:2])
        self.assertEqual(metadata["strategy"], "head8_middle4_tail8")
        self.assertEqual(metadata["sampled_data_rows"], 20)
        self.assertEqual(metadata["sampled_data_indexes"][:8], list(range(8)))
        self.assertEqual(metadata["sampled_data_indexes"][-8:], list(range(92, 100)))

    def test_large_pin_table_uses_stratified_middle_without_mutating_source(self):
        rows = [["PARENT"], ["PIN"]] + [[str(index)] for index in range(880)]
        original = [list(row) for row in rows]

        sampled, metadata = build_semantic_table_sample(rows, header_index=1)

        self.assertEqual(rows, original)
        self.assertEqual(sampled[:2], rows[:2])
        self.assertEqual(metadata["strategy"], "head6_stratified8_tail6")
        self.assertEqual(metadata["sampled_data_rows"], 20)
        self.assertEqual(metadata["sampled_data_indexes"][:6], list(range(6)))
        self.assertEqual(metadata["sampled_data_indexes"][-6:], list(range(874, 880)))
        self.assertEqual(len(metadata["sampled_data_indexes"][6:-6]), 8)

    def test_orchestrator_sends_sample_but_keeps_complete_rows_for_extraction(self):
        captured = []
        complete_rows = [["PIN"]] + [[str(index)] for index in range(300)]
        prepared = [{
            "table_id": 0,
            "table": SimpleNamespace(title="Large pin table"),
            "headers": ["PIN"],
            "rows": complete_rows,
            "header_index": 0,
            "data_rows": complete_rows[1:],
            "header_paths": [],
            "name_layout": NameColumnLayout(mode="single_name"),
        }]

        def fake_batch(tables):
            captured.extend(tables)
            return {
                "0": {
                    "should_extract": True,
                    "columns": [{"column_index": 0, "field": "pin_no"}],
                }
            }

        with patch(
            "extract.pin_package_extractor.find_special_table_match",
            return_value=None,
        ), patch(
            "extract.semantic_classifier.classify_table_schema_batch",
            side_effect=fake_batch,
        ):
            result = decide_all_tables(prepared, True, False)

        self.assertTrue(result[0].should_extract)
        self.assertEqual(len(captured[0]["table_rows"]), 21)
        self.assertEqual(captured[0]["sampling"]["total_data_rows"], 300)
        self.assertEqual(len(prepared[0]["rows"]), 301)

    def test_schema_batch_preserves_request_ids_when_model_reorders_results(self):
        captured = {}

        def fake_call(payload, **kwargs):
            captured.update(payload)
            return {
                "results": [
                    {
                        "request_id": str(index),
                        "should_extract": True,
                        "columns": [{"column_index": 0, "field": "pin_no"}],
                    }
                    for index in reversed(range(4))
                ]
            }

        tables = [
            {
                "request_id": str(index),
                "title": f"Table {index}",
                "headers": ["COL"],
                "table_rows": [[str(index)]],
            }
            for index in range(4)
        ]
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(
            "extract.semantic_classifier.call_model_json",
            side_effect=fake_call,
        ):
            result = classify_table_schema_batch(tables)

        self.assertEqual([item["request_id"] for item in captured["tables"]], ["0", "1", "2", "3"])
        self.assertEqual(set(result), {"0", "1", "2", "3"})

    def test_pin_table_orchestrator_chunks_nine_tables_as_four_four_one(self):
        batch_sizes = []

        def fake_batch(tables):
            batch_sizes.append(len(tables))
            return {
                item["request_id"]: {
                    "should_extract": True,
                    "columns": [{"column_index": 0, "field": "pin_no"}],
                }
                for item in tables
            }

        prepared = [
            {
                "table_id": index,
                "table": SimpleNamespace(title=f"Table {index}"),
                "headers": ["PIN"],
                "rows": [["PIN"], [str(index)]],
                "data_rows": [[str(index)]],
                "header_paths": [],
                "name_layout": NameColumnLayout(mode="single_name"),
            }
            for index in range(9)
        ]
        with patch(
            "extract.pin_package_extractor.find_special_table_match",
            return_value=None,
        ), patch(
            "extract.semantic_classifier.classify_table_schema_batch",
            side_effect=fake_batch,
        ):
            result = decide_all_tables(prepared, True, False)

        self.assertEqual(sorted(batch_sizes), [1, 4, 4])
        self.assertEqual(set(result), set(range(9)))
        self.assertTrue(all(decision.should_extract for decision in result.values()))

    def test_pin_table_batch_failure_retries_each_table_independently(self):
        call_sizes = []

        def fake_batch(tables):
            call_sizes.append(len(tables))
            if len(tables) > 1:
                raise TimeoutError("batch timeout")
            item = tables[0]
            return {
                item["request_id"]: {
                    "should_extract": True,
                    "columns": [{"column_index": 0, "field": "pin_no"}],
                }
            }

        prepared = [
            {
                "table_id": index,
                "table": SimpleNamespace(title=f"Table {index}"),
                "headers": ["PIN"],
                "rows": [["PIN"], [str(index)]],
                "data_rows": [[str(index)]],
                "header_paths": [],
                "name_layout": NameColumnLayout(mode="single_name"),
            }
            for index in range(4)
        ]
        with patch(
            "extract.pin_package_extractor.find_special_table_match",
            return_value=None,
        ), patch(
            "extract.semantic_classifier.classify_table_schema_batch",
            side_effect=fake_batch,
        ):
            result = decide_all_tables(prepared, True, False)

        self.assertEqual(call_sizes.count(4), 1)
        self.assertEqual(call_sizes.count(1), 4)
        self.assertEqual(set(result), set(range(4)))
        self.assertTrue(all(decision.should_extract for decision in result.values()))

    def test_package_catalog_orchestrator_chunks_nine_tables_as_four_four_one(self):
        batch_sizes = []

        def fake_batch(tables, **kwargs):
            batch_sizes.append(len(tables))
            return {
                request_id: {
                    "is_package_summary": False,
                    "table_role": "irrelevant",
                    "header_row_index": 0,
                    "columns": [],
                }
                for request_id, _ in tables
            }

        tables = [
            PackageCatalogTable(
                table_id=index,
                page_idx=index,
                title=f"Table {index}",
                group_context="",
                current_chapter_titles=(),
                headers=("COL",),
                rows=((str(index),),),
            )
            for index in range(9)
        ]
        with patch(
            "extract.semantic_classifier.classify_package_catalog_tables",
            side_effect=fake_batch,
        ):
            entries, diagnostics = classify_package_catalog_candidates(
                tables,
                source_name="sample.pdf",
                target_tables=(),
            )

        self.assertEqual(sorted(batch_sizes), [1, 4, 4])
        self.assertEqual(entries, [])
        self.assertEqual(
            sum(item.get("status") == "rejected" for item in diagnostics),
            9,
        )


if __name__ == "__main__":
    unittest.main()
