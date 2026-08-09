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
from extract.pin_package_extractor import decide_all_tables
from extract.semantic_classifier import classify_table_schema_batch
from extract.table_header_structure import NameColumnLayout


class SemanticBatchingTests(unittest.TestCase):
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
