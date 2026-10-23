"""验证 Nexperia 表头、范围、端子与字段缺省，不运行解析或模型。"""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from extract.pin_package_extractor import TableCandidate, extract_pin_package_info_from_table_candidates, normalize_pin_record
from extract.test.test_abov_vendor_tables import table, pins
from extract.vendors import get_vendor_profile
from extract.vendors.nexperia.package_handlers import resolve_nexperia_scopes
from extract.vendors.nexperia.table_handlers import match_nexperia_table, prepare_nexperia_tables, repair_nexperia_rows


def grouped(headers, sections, title="Table 3. Pinning"):
    from html import escape
    html = "<table><tr>" + "".join("<td>" + escape(h) + "</td>" for h in headers) + "</tr>"
    for scope, rows in sections:
        html += f'<tr><td colspan="{len(headers)}">{escape(scope)}</td></tr>'
        for row in rows:
            html += "<tr>" + "".join("<td>" + escape(value) + "</td>" for value in row) + "</tr>"
    return TableCandidate(html=html + "</table>", page_idx=1, title=title)


class NexperiaVendorTableTest(unittest.TestCase):
    headers = ["Pin", "Symbol", "Description", "Simplified outline", "Graphic symbol"]

    def extract(self, candidates, semantic=False):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "NEXPERIA"}):
            return extract_pin_package_info_from_table_candidates(candidates, use_semantic_classifier=semantic)

    def test_registration_and_hook_isolation(self):
        self.assertEqual(get_vendor_profile("Nexperia").canonical_name, "NEXPERIA")
        for name in ("TI", "ST", "ABOV", "ELAN", "GIGA", "INFINEON"):
            self.assertIsNone(get_vendor_profile(name).record_finalizer)
            self.assertNotEqual(get_vendor_profile(name).source_table_repairer, prepare_nexperia_tables)

    def test_explicit_symbol_and_description_without_invented_type(self):
        result = self.extract([table(self.headers, [["1", "K", "Cathode", "outline", "drawing"],
                                                    ["2", "A", "Anode", "", ""]], "Pinning information")])
        self.assertEqual(pins(result[0]), [{"pin_no": "1", "pin_name": "K", "description": "Cathode"},
                                         {"pin_no": "2", "pin_name": "A", "description": "Anode"}])

    def test_description_only_does_not_become_pin_name_or_reserved(self):
        result = self.extract([table(["PIN", "DESCRIPTION"], [["1", "cathode"], ["2", "anode"]], "PINNING")])
        self.assertEqual(pins(result[0]), [{"pin_no": "1", "description": "cathode"},
                                         {"pin_no": "2", "description": "anode"}])

    def test_explicit_type_is_retained(self):
        result = self.extract([table(["Pin", "Symbol", "Type", "Description"], [["1", "G", "I", "gate"]], "Pinning")])
        self.assertEqual(pins(result[0])[0]["type"], "I")

    def test_nc_definition_is_not_dropped(self):
        result = self.extract([table(["Pin", "Description"], [["1", "anode"], ["2", "not connected"], ["3", "cathode"]], "Pinning")])
        self.assertEqual([r["pin_no"] for r in pins(result[0])], ["1", "2", "3"])
        self.assertEqual(pins(result[0])[1]["description"], "not connected")

    def test_mounting_base_is_a_terminal_not_a_missing_number(self):
        result = self.extract([table(self.headers, [["1", "S", "source", "", ""], ["2", "S", "source", "", ""],
                                                    ["3", "S", "source", "", ""], ["4", "G", "gate", "", ""],
                                                    ["mb", "D", "mounting base; connected to drain", "", ""]], "Pinning information")])
        self.assertEqual([r["pin_no"] for r in pins(result[0])], ["1", "2", "3", "4", "mb"])
        self.assertEqual(pins(result[0])[-1]["pin_name"], "D")

    def test_multi_model_groups_remain_four_mapping_slots(self):
        headers = ["Pin", "Description", "Simplified outline", "Graphic symbol"]
        sections = [("TYPE_A; TYPE_B", [["1", "anode", "", ""], ["2", "not connected", "", ""], ["3", "cathode", "", ""]]),
                    ("TYPE_C; TYPE_D", [["1", "cathode", "", ""], ["2", "anode", "", ""]]),
                    ("TYPE_E", [["1", "cathode", "", ""], ["2", "anode", "", ""]]),
                    ("TYPE_F; TYPE_G", [[str(i), f"terminal {i}", "", ""] for i in range(1, 7)])]
        result = self.extract([grouped(headers, sections)])
        self.assertEqual([len(pins(p)) for p in result], [3, 2, 2, 6])
        self.assertEqual(len({p["pkg"] for p in result}), 4)
        self.assertTrue(all("pin_name" not in r for p in result for r in pins(p)))

    def test_package_group_memberships_are_not_merged(self):
        headers = ["Pin", "Description", "Simplified outline", "Graphic symbol"]
        sections = [("SOT223", [["1", "base", "", ""], ["2", "collector", "", ""], ["3", "emitter", "", ""], ["4", "collector", "", ""]]),
                    ("SOT89", [["1", "emitter", "", ""], ["2", "collector", "", ""], ["3", "base", "", ""]]),
                    ("SOT1061", [["1", "base", "", ""], ["2", "emitter", "", ""], ["3", "collector", "", ""]])]
        result = self.extract([grouped(headers, sections)])
        self.assertEqual([len(pins(p)) for p in result], [4, 3, 3])
        self.assertEqual([pins(p)[0]["description"] for p in result], ["base", "emitter", "base"])

    def test_same_explicit_group_continuations_use_same_slot(self):
        headers = ["Pin", "Description"]
        result = self.extract([grouped(headers, [("SOT223", [["1", "base"]])]),
                               grouped(headers, [("SOT223", [["2", "collector"]])], "Pinning (continued)"),
                               grouped(headers, [("SOT89", [["1", "emitter"]])])])
        self.assertEqual([len(pins(p)) for p in result], [2, 1])

    def test_number_reset_alone_does_not_create_group(self):
        candidate = table(["Pin", "Description"], [["1", "first"], ["2", "second"], ["1", "another"]], "Pinning")
        self.assertEqual(len(prepare_nexperia_tables([candidate])), 1)

    def test_alias_symbols_are_split_with_their_proven_descriptions(self):
        result = self.extract([table(["Pin", "Symbol", "Description"],
                                     [["3", "A3; K4", "anode (diode 3), cathode (diode 4)"],
                                      ["6", "K1; A2", "cathode (diode 1), anode (diode 2)"]], "Pinning")])
        self.assertEqual([(r["pin_no"], r["pin_name"], r["description"]) for r in pins(result[0])],
                         [("3", "A3", "anode (diode 3)"), ("3", "K4", "cathode (diode 4)"),
                          ("6", "K1", "cathode (diode 1)"), ("6", "A2", "anode (diode 2)")])

    def test_unproven_description_pairing_keeps_complete_source_description(self):
        rows = [["1", "DATA0; DATA1", "shared signal, selectable mode"]]
        fixed = repair_nexperia_rows("Pinning", ["Pin", "Symbol", "Description"], rows)
        self.assertEqual([r[1] for r in fixed], ["DATA0", "DATA1"])
        self.assertEqual([r[2] for r in fixed], [rows[0][2]] * 2)

    def test_diodes_with_mismatching_indexes_do_not_guess_clause_pairing(self):
        description = "anode (diode 4), cathode (diode 3)"
        fixed = repair_nexperia_rows("Pinning", ["Pin", "Symbol", "Description"], [["1", "A3; K4", description]])
        self.assertEqual([r[2] for r in fixed], [description, description])

    def test_source_composite_slash_name_is_not_arbitrarily_split(self):
        result = self.extract([table(["Pin", "Symbol", "Description"], [["1", "A/K", "shared function"]], "Pinning")])
        self.assertEqual(pins(result[0])[0]["pin_name"], "A/K")

    def test_footnote_and_graphic_columns_do_not_become_pins(self):
        candidate = table(["Pin", "Description", "", "Simplified outline", "Graphic symbol"],
                          [["1", "cathode", "[1]", "1 2 3", "drawing"], ["2", "anode", "", "", ""],
                           ["", "", "", "Transparent top view", ""]], "Pinning")
        result = self.extract([candidate])
        self.assertEqual(pins(result[0]), [{"pin_no": "1", "description": "cathode"}, {"pin_no": "2", "description": "anode"}])

    def test_non_pinning_table_is_not_rewritten_or_matched(self):
        candidate = table(["Symbol", "Parameter", "Min", "Max"], [["VR", "voltage", "0", "75"]], "Limiting values")
        self.assertIs(prepare_nexperia_tables([candidate])[0], candidate)
        self.assertIsNone(match_nexperia_table("Characteristics", ["Pin", "Symbol", "Description"], [["1", "A", "text"]]))

    def test_source_metadata_is_preserved(self):
        candidate = replace(table(["Pin", "Description"], [["1", "anode"]], "Pinning"), page_idx=4,
                            group_context="Chapter", current_chapter_titles=("2 Pinning information",))
        fixed = prepare_nexperia_tables([candidate])[0]
        self.assertEqual(fixed.page_idx, 4)
        self.assertEqual(fixed.current_chapter_titles, candidate.current_chapter_titles)
        self.assertIn("Chapter", fixed.group_context)

    def test_other_manufacturers_keep_existing_reserved_default(self):
        for vendor in ("TI", "ST", "ABOV", "ELAN", "GIGA", "INFINEON"):
            with patch.dict("os.environ", {"EXTRACT_VENDOR": vendor}):
                self.assertEqual(normalize_pin_record({"pin_no": "1"})["pin_name"], "Reserved")
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "NEXPERIA"}):
            self.assertNotIn("pin_name", normalize_pin_record({"pin_no": "1", "description": "anode"}))
            self.assertEqual(normalize_pin_record({"pin_no": "1", "pin_name": "Reserved"})["pin_name"], "Reserved")

    def test_multiple_unscoped_tables_do_not_force_slots(self):
        targets = [SimpleNamespace(table_id=i, title="Pinning", headers=["Pin number", "Description"]) for i in range(2)]
        plans = {i: SimpleNamespace(is_multi_package=False) for i in range(2)}
        self.assertIsNone(resolve_nexperia_scopes(targets, plans))

    def test_clear_pinning_table_does_not_call_models_even_with_semantic_enabled(self):
        with patch("extract.semantic_classifier.classify_table_schema_batch", side_effect=AssertionError("不应调用表分类模型")), \
             patch("extract.semantic_classifier.classify_package_catalog_tables", side_effect=AssertionError("不应调用目录模型")):
            result = self.extract([table(["Pin", "Description"], [["1", "anode"]], "Pinning")], semantic=True)
        self.assertEqual(pins(result[0])[0]["pin_no"], "1")


if __name__ == "__main__":
    unittest.main()
