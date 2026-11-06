"""验证图片范围、字段结构、章节层级、文件安全和两种 MinerU 格式。"""

import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from export_images import ExportError, collect, export, find_source, main, section_number


class ExportImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parse = self.root / "parse"
        self.parse.mkdir()
        (self.parse / "images").mkdir()
        for name in ("one.png", "two.jpg"):
            (self.parse / "images" / name).write_bytes(b"test asset")
        self.source = self.parse / "document_content_list.json"
        self.output = self.root / "exports" / "images.json"

    def write(self, data, path=None):
        path = path or self.source
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def image(self, path="images/one.png", caption=None):
        return {"type": "image", "img_path": path, "image_caption": caption or []}

    def heading(self, title, level=1):
        return {"type": "text", "text": title, "text_level": level}

    def test_three_fields_and_relative_path_preserve_original_format(self):
        self.write([self.image("images/two.jpg", ["Figure 1", "Description"])])
        records = export(self.source, self.output)
        self.assertEqual(records, [{"image_path": "../parse/images/two.jpg", "image_title": "Figure 1 Description", "section": ""}])
        self.assertTrue((self.output.parent / records[0]["image_path"]).is_file())
        self.assertEqual(json.loads(self.output.read_text()), records)

    def test_only_images_not_table_formula_header_or_footer(self):
        data = [{"type": kind, "img_path": "images/not-present.png", "text": "Ignored"}
                for kind in ("table", "equation", "header", "footer", "page_number")]
        self.write(data + [self.image()])
        self.assertEqual(len(collect(self.source, self.output)), 1)

    def test_numbered_hierarchy_overrides_flat_mineru_levels(self):
        self.write([self.heading("2 Interface Description"), self.heading("2.1 Pin Diagram"), self.image()])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "2 Interface Description > 2.1 Pin Diagram")

    def test_sibling_replaces_previous_subsection(self):
        self.write([self.heading("2 Interface"), self.heading("2.1 First"), self.heading("2.2 Second"), self.image()])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "2 Interface > 2.2 Second")

    def test_new_chapter_does_not_keep_old_ancestors(self):
        self.write([self.heading("2 Interface"), self.heading("2.1 Pins"), self.heading("3 Operations"), self.image()])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "3 Operations")

    def test_missing_parent_not_invented(self):
        self.write([self.heading("3.2 Operations"), self.image()])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "3.2 Operations")

    def test_numeric_ancestor_can_be_reused_after_unnumbered_heading(self):
        self.write([self.heading("1 Overview"), self.heading("1.2 Features"), self.heading("Main features:"),
                    self.heading("1.3 Block Diagram"), self.image()])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "1 Overview > 1.3 Block Diagram")

    def test_unnumbered_headings_follow_explicit_levels(self):
        self.write([self.heading("总览"), self.heading("框图", 2), self.image(), self.heading("接口", 2), self.image()])
        self.assertEqual([r["section"] for r in collect(self.source, self.output)], ["总览 > 框图", "总览 > 接口"])

    def test_body_text_does_not_change_section_or_guess_caption(self):
        self.write([self.heading("2 Pins"), {"type": "text", "text": "Figure 7 Not an attached caption"}, self.image()])
        record = collect(self.source, self.output)[0]
        self.assertEqual(record["section"], "2 Pins")
        self.assertEqual(record["image_title"], "")

    def test_image_before_any_heading_has_empty_section(self):
        self.write([self.image(), self.heading("2 Pins")])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "")

    def test_multi_level_sections_and_appendix(self):
        self.assertEqual(section_number("Appendix A References"), ("A",))
        self.write([self.heading("Appendix A References"), self.heading("A.1 Examples"), self.heading("A.1.1 Circuit"), self.image()])
        self.assertEqual(collect(self.source, self.output)[0]["section"], "Appendix A References > A.1 Examples > A.1.1 Circuit")

    def test_missing_caption_does_not_drop_image(self):
        self.write([self.image(), self.image("images/two.jpg")])
        self.assertEqual([r["image_title"] for r in collect(self.source, self.output)], ["", ""])

    def test_repeated_asset_occurrences_are_not_deduplicated(self):
        self.write([self.heading("1 One"), self.image(), self.heading("2 Two"), self.image()])
        self.assertEqual([r["section"] for r in collect(self.source, self.output)], ["1 One", "2 Two"])

    def test_caption_html_and_footnotes(self):
        image = self.image(caption=["Figure 1 <b>Example</b><br>续行 &amp; 注释"])
        image["image_footnote"] = ["Not part of caption"]
        self.write([image])
        self.assertEqual(collect(self.source, self.output)[0]["image_title"], "Figure 1 Example 续行 & 注释")

    def test_v2_uses_rich_text_and_attached_image_source(self):
        def rich(value): return [{"type": "text", "content": value}]
        self.write([[{"type": "title", "content": {"title_content": rich("2 Interface"), "level": 1}}],
                    [{"type": "title", "content": {"title_content": rich("2.1 Pins"), "level": 1}},
                     {"type": "image", "content": {"image_source": {"path": "images/one.png"},
                      "image_caption": [{"type": "text", "content": "Figure 1 Ex"}, {"type": "text", "content": "ample"}]}}]])
        record = collect(self.source, self.output)[0]
        self.assertEqual(record["image_title"], "Figure 1 Example")
        self.assertEqual(record["section"], "2 Interface > 2.1 Pins")

    def test_directory_prefers_v1_over_same_document_v2(self):
        self.write([])
        self.write([], self.parse / "document_content_list_v2.json")
        self.assertEqual(find_source(self.parse), self.source.resolve())

    def test_directory_rejects_multiple_documents(self):
        self.write([])
        self.write([], self.parse / "other_content_list.json")
        with self.assertRaises(ExportError): find_source(self.parse)

    def test_directory_rejects_missing_source(self):
        with self.assertRaises(ExportError): find_source(self.parse)
        with self.assertRaises(ExportError): find_source(self.root / "missing")

    def test_empty_document_outputs_empty_list(self):
        self.write([])
        self.assertEqual(export(self.source, self.output), [])

    def test_missing_image_fails_without_partial_output(self):
        self.write([self.image(), self.image("images/missing.png")])
        with self.assertRaises(ExportError): export(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_image_path_fails(self):
        self.write([{"type": "image"}])
        with self.assertRaises(ExportError): export(self.source, self.output)

    def test_source_and_image_cannot_be_overwritten(self):
        self.write([self.image()])
        with self.assertRaises(ExportError): export(self.source, self.source, overwrite=True)
        asset = self.parse / "images" / "one.png"
        with self.assertRaises(ExportError): export(self.source, asset, overwrite=True)
        self.assertEqual(asset.read_bytes(), b"test asset")

    def test_existing_output_requires_overwrite_and_survives_error(self):
        self.write([self.image()])
        export(self.source, self.output)
        original = self.output.read_bytes()
        with self.assertRaises(ExportError): export(self.source, self.output)
        self.write([self.image("images/missing.png")])
        with self.assertRaises(ExportError): export(self.source, self.output, overwrite=True)
        self.assertEqual(self.output.read_bytes(), original)
        self.write([])
        export(self.source, self.output, overwrite=True)
        self.assertEqual(json.loads(self.output.read_text()), [])

    def test_rejects_middle_json_and_malformed_content(self):
        for data in ({"pdf_info": []}, [17], [[{"type": "image", "content": []}]]):
            self.write(data)
            with self.assertRaises(ExportError): export(self.source, self.output)

    def test_absolute_asset_path_is_exported_as_relative(self):
        self.write([self.image(str(self.parse / "images" / "one.png"))])
        record = collect(self.source, self.output)[0]
        self.assertFalse(Path(record["image_path"]).is_absolute())

    def test_cli_success_and_failure_exit_codes(self):
        self.write([self.image()])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["-i", str(self.source), "-o", str(self.output)]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["-i", str(self.source), "-o", str(self.output)]), 1)


if __name__ == "__main__":
    unittest.main()
