import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace

from extract.pin_package_extractor import TableCandidate, extract_pin_package_info_from_table_candidates
from extract.pin_package_extractor import extract_pin_package_info_from_markdown_json_file, extract_pin_package_info_from_middle_json_file
from extract.test.test_abov_vendor_tables import table, pins
from extract.vendors import get_vendor_profile
from extract.vendors.elan.native_tables import recover_elan_tables, scope_from_title
from extract.vendors.elan.table_handlers import numbers, repair_elan_rows


def native_pdf(path, pages):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

    writer = PdfWriter()
    for title, rows, header in pages:
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                                NameObject("/Subtype"): NameObject("/Type1"),
                                                NameObject("/BaseFont"): NameObject("/Helvetica")})})})
        content = [f"BT /F1 10 Tf 60 735 Td ({title}) Tj ET"]
        data = ([["Symbol", "Pin No.", "Type", "Function"]] if header else []) + rows
        xs = [60, 160, 230, 280, 550]; top = 720
        for x in xs:
            content.append(f"{x} {top - 30 * len(data)} .6 {30 * len(data)} re f")
        for i in range(len(data) + 1):
            content.append(f"60 {top - 30 * i} 490 .6 re f")
        for i, row in enumerate(data):
            for x, value in zip(xs, row):
                content.append(f"BT /F1 9 Tf {x + 4} {top - 20 - 30 * i} Td ({value}) Tj ET")
        stream = DecodedStreamObject(); stream.set_data("\n".join(content).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(str(path))


class ElanVendorTableTest(unittest.TestCase):
    def extract(self, tables, **kwargs):
        with patch.dict("os.environ", {"EXTRACT_VENDOR": "ELAN"}):
            return extract_pin_package_info_from_table_candidates(tables, **kwargs)

    def test_registry_isolates_source_repair(self):
        self.assertEqual(get_vendor_profile("Elan").canonical_name, "ELAN")
        for vendor in ["TI", "ST", "ABOV"]:
            self.assertIsNone(get_vendor_profile(vendor).source_table_repairer)

    def test_chinese_headers_and_tilde_ranges(self):
        result = self.extract([table(["符号", "引脚序号", "类型", "功能"],
                                     [["P60~P62", "6~8", "I/O", "GPIO"]])])
        self.assertEqual(pins(result[0]), [dict(pin_no=str(n), pin_name=f"P6{n-6}", type="I/O", description="GPIO") for n in range(6,9)])

    def test_english_symbol_header_is_not_electrical_parameter(self):
        result = self.extract([table(["Symbol", "Pin No.", "Type", "Function"], [["VDD", "4", "-", "Supply"]])])
        self.assertEqual(pins(result[0])[0]["pin_name"], "VDD")

    def test_reverse_and_multiple_ranges_preserve_source_order(self):
        self.assertEqual(numbers("17 ~ 14,\n5"), ["17", "16", "15", "14", "5"])
        result = self.extract([table(["Pin name", "Pin number", "Pin type"], [["P70~P73,\nP77", "17~14,\n5", "I/O"]])])
        self.assertEqual([(p["pin_no"], p["pin_name"]) for p in pins(result[0])], [("17","P70"),("16","P71"),("15","P72"),("14","P73"),("5","P77")])

    def test_names_numbers_and_types_expand_together(self):
        result = self.extract([table(["Pin name", "Pin number", "Pin type"],
                                     [["NEG\nPOS\nOUT", "26\n27\n28", "I\nI\nO"]])])
        self.assertEqual([(p["pin_no"],p["pin_name"],p["type"]) for p in pins(result[0])], [("26","NEG","I"),("27","POS","I"),("28","OUT","O")])

    def test_type_lines_cover_pairs_of_names(self):
        result = self.extract([table(["Pin name", "Pin number", "Pin type"],
                                     [["NEG1,POS1\nOUT1,OUT2", "4,3\n10,11", "I\nO"]])])
        self.assertEqual([p["type"] for p in pins(result[0])], ["I","I","O","O"])

    def test_same_number_never_aggregates_different_functions(self):
        result = self.extract([table(["Pin name", "Pin number", "Pin type", "Description"],
                                     [["GPIO", "1", "I/O", "GPIO mode"],["ADC", "1", "I", "Analog mode"],["TIMER", "1", "O", "Timer mode"]])])
        self.assertEqual([(p["pin_name"],p["type"],p["description"]) for p in pins(result[0])], [("GPIO","I/O","GPIO mode"),("ADC","I","Analog mode"),("TIMER","O","Timer mode")])

    def test_cardinality_mismatch_not_guessed(self):
        row = ["P50~P57", "10~11\n15~16\n25~26", "I/O"]
        with self.assertWarnsRegex(UserWarning,"no guessed pairing"):
            fixed = repair_elan_rows("Pins", ["Pin name","Pin number","Pin type"],[row])
        self.assertEqual(fixed, [row])

    def test_concatenated_digits_not_split_without_pdf_coordinates(self):
        self.assertEqual(numbers("943101"), ["943101"])
        self.assertEqual(numbers("262728"), ["262728"])
        self.assertIsNone(numbers("1~417~20"))
        self.assertIsNone(numbers("1~5000"))

    def test_scope_requires_explicit_subsection_or_pin_description_title(self):
        self.assertEqual(scope_from_title("4.1 IC100A"), "4.1 IC100A")
        self.assertEqual(scope_from_title("Table 2 Pin Description- IC200B"), "Pin Description- IC200B")
        self.assertEqual(scope_from_title("IC100A Datasheet"), "")

    def test_independent_sections_do_not_share_catalog_slot(self):
        headers = ["Pin name","Pin number","Pin type"]
        result = self.extract([table(headers,[["SIGNAL_A","1","I"]],"4.1 IC100A"),
                               table(headers,[["SIGNAL_B","1","O"]],"4.2 IC200B")])
        self.assertEqual(len(result),2)
        self.assertEqual([pins(p)[0]["pin_name"] for p in result], ["SIGNAL_A","SIGNAL_B"])

    def test_continuation_preserves_scope(self):
        headers = ["Pin name","Pin number","Pin type"]
        result = self.extract([table(headers,[["SIGNAL_A","1","I"]],"4.1 IC100A"),
                               table(headers,[["SIGNAL_B","2","O"]],"4.1 IC100A"),
                               table(headers,[["OTHER","1","I"]],"4.2 IC200B")])
        self.assertEqual([len(pins(p)) for p in result], [2,1])

    def test_no_pdf_keeps_original_candidates(self):
        candidates = [table(["Symbol","Pin No.","Type"],[["VDD","4","-"]])]
        self.assertIs(recover_elan_tables(candidates, None), candidates)

    def test_markdown_file_passes_source_path_to_vendor_hook(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/"document.json"
            candidate = table(["Symbol","Pin No.","Type"],[["GPIO","1","I"]])
            path.write_text(json.dumps({"markdown": candidate.html}))
            hook = Mock(return_value=[candidate])
            profile = replace(get_vendor_profile("ELAN"), source_table_repairer=hook)
            with patch("extract.pin_package_extractor.get_current_vendor_profile", return_value=profile):
                extract_pin_package_info_from_markdown_json_file(path, source_pdf_path=Path(tmp)/"source.pdf")
            self.assertEqual(hook.call_args.args[1], Path(tmp)/"source.pdf")

    def test_middle_file_passes_source_path_to_vendor_hook(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/"document_middle.json"
            candidate = table(["Symbol","Pin No.","Type"],[["GPIO","1","I"]])
            path.write_text(json.dumps({"pdf_info": []}))
            hook = Mock(return_value=[candidate])
            profile = replace(get_vendor_profile("ELAN"), source_table_repairer=hook)
            with patch("extract.pin_package_extractor.get_current_vendor_profile", return_value=profile):
                extract_pin_package_info_from_middle_json_file(path, source_pdf_path=Path(tmp)/"source.pdf")
            self.assertEqual(hook.call_args.args[1], Path(tmp)/"source.pdf")

    def test_native_grid_recovers_missing_header_and_missing_rows(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/"arbitrary.pdf"
            native_pdf(path,[("4.1 IC100A",[["GPIO0","1","I/O","GPIO"],["VDD","2","-","Supply"]],True)])
            damaged = TableCandidate(html="<table><tr><td></td></tr><tr><td>GPIO0</td><td>1</td><td>I/O</td><td>GPIO</td></tr></table>",page_idx=0,title="Pins")
            result = self.extract([damaged], source_pdf_path=path)
            self.assertEqual([(p["pin_no"],p["pin_name"]) for p in pins(result[0])],[("1","GPIO0"),("2","VDD")])

    def test_native_continuation_without_header_retains_first_section(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"arbitrary.pdf"
            native_pdf(path,[("4.1 IC100A",[["GPIO0","1","I/O","GPIO"]],True),
                             ("",[["GPIO1","2","I/O","GPIO"]],False)])
            candidate=table(["Symbol","Pin No.","Type","Function"],[["GPIO0","1","I/O","GPIO"]])
            result=self.extract([candidate],source_pdf_path=path)
            self.assertEqual([p["pin_no"] for p in pins(result[0])],["1","2"])

    def test_two_native_subsections_remain_separate(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"arbitrary.pdf"
            native_pdf(path,[("4.1 IC100A",[["FIRST","1","I","Input"]],True),
                             ("4.2 IC200B",[["SECOND","1","O","Output"]],True)])
            candidates=[table(["Symbol","Pin No.","Type","Function"],[["FIRST","1","I","Input"]])]
            result=self.extract(candidates,source_pdf_path=path)
            self.assertEqual(len(result),2)
            self.assertEqual([pins(p)[0]["pin_name"] for p in result],["FIRST","SECOND"])


if __name__=="__main__":
    unittest.main()
