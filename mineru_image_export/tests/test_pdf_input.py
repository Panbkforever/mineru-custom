"""模拟 MinerU 子进程验证 PDF 全流程，不实际解析 PDF 或加载模型。"""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from errors import ExportError
from export_images import main
from pdf_input import parse_pdf


class PDFInputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "文档 with spaces.PDF"
        self.pdf.write_bytes(b"%PDF-1.7\nsynthetic test fixture")
        self.output = self.root / "export" / "images.json"
        self.parsed = self.root / "parsed"

    def fake_mineru(self, command, check):
        self.assertTrue(check)
        folder = Path(command[command.index("-o") + 1]) / "document" / "hybrid_auto"
        (folder / "images").mkdir(parents=True)
        (folder / "images" / "one.png").write_bytes(b"image fixture")
        data = [{"type": "text", "text": "2 Interface", "text_level": 1},
                {"type": "text", "text": "2.1 Pin Diagram", "text_level": 1},
                {"type": "image", "img_path": "images/one.png", "image_caption": ["Figure 1 Example"]}]
        (folder / "document_content_list.json").write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def run_cli(self, extra=None):
        args = ["-i", str(self.pdf), "-o", str(self.output), "--parse-output-dir", str(self.parsed)] + (extra or [])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(args)

    def test_pdf_to_mineru_then_three_field_json(self):
        with patch("pdf_input.subprocess.run", side_effect=self.fake_mineru) as run:
            self.assertEqual(self.run_cli(), 0)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-m", "mineru.cli.client"])
        self.assertEqual(command[command.index("-p") + 1], str(self.pdf.resolve()))
        self.assertEqual(command[command.index("-b") + 1], "hybrid-auto-engine")
        self.assertEqual(command[-2:], ["--image-analysis", "false"])
        result = json.loads(self.output.read_text())
        self.assertEqual(set(result[0]), {"image_path", "image_title", "section"})
        self.assertEqual(result[0]["section"], "2 Interface > 2.1 Pin Diagram")
        self.assertTrue((self.output.parent / result[0]["image_path"]).is_file())

    def test_options_and_custom_python_pass_as_separate_arguments(self):
        with patch("pdf_input.subprocess.run", side_effect=self.fake_mineru) as run:
            self.assertEqual(self.run_cli(["-b", "pipeline", "-m", "ocr", "--lang", "en",
                                           "--mineru-python", "/path with spaces/python"]), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/path with spaces/python")
        self.assertEqual(command[command.index("-b") + 1], "pipeline")
        self.assertEqual(command[command.index("-m", 3) + 1], "ocr")
        self.assertEqual(command[command.index("--lang") + 1], "en")

    def test_existing_output_stops_before_parsing(self):
        self.output.parent.mkdir()
        self.output.write_text("old output")
        with patch("pdf_input.subprocess.run") as run:
            self.assertEqual(self.run_cli(), 1)
        run.assert_not_called()
        self.assertEqual(self.output.read_text(), "old output")

    def test_successful_overwrite_reruns_parse(self):
        self.output.parent.mkdir()
        self.output.write_text("old output")
        with patch("pdf_input.subprocess.run", side_effect=self.fake_mineru) as run:
            self.assertEqual(self.run_cli(["--overwrite"]), 0)
        run.assert_called_once()
        self.assertEqual(len(json.loads(self.output.read_text())), 1)

    def test_failed_parse_preserves_existing_output(self):
        self.output.parent.mkdir()
        self.output.write_text("old output")
        with patch("pdf_input.subprocess.run", side_effect=subprocess.CalledProcessError(2, ["mineru"])):
            self.assertEqual(self.run_cli(["--overwrite"]), 1)
        self.assertEqual(self.output.read_text(), "old output")

    def test_missing_interpreter_returns_readable_error(self):
        with patch("pdf_input.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(self.run_cli(), 1)
        self.assertFalse(self.output.exists())

    def test_no_new_content_list_cannot_reuse_old_output(self):
        old = self.parsed / "old"
        old.mkdir(parents=True)
        (old / "stale_content_list.json").write_text("[]")
        with patch("pdf_input.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(self.run_cli(), 1)
        self.assertFalse(self.output.exists())

    def test_each_parse_has_separate_persistent_output_folder(self):
        with patch("pdf_input.subprocess.run", side_effect=self.fake_mineru), contextlib.redirect_stdout(io.StringIO()):
            first = parse_pdf(self.pdf, self.parsed)
            second = parse_pdf(self.pdf, self.parsed)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())

    def test_missing_or_invalid_pdf_never_starts_mineru(self):
        with patch("pdf_input.subprocess.run") as run:
            self.pdf.unlink()
            self.assertEqual(self.run_cli(), 1)
            self.pdf.write_bytes(b"not a PDF")
            self.assertEqual(self.run_cli(), 1)
            with self.assertRaises(ExportError):
                parse_pdf(self.output, self.parsed)
        run.assert_not_called()

    def test_cannot_overwrite_input_pdf_even_with_overwrite_flag(self):
        self.output = self.pdf
        with patch("pdf_input.subprocess.run") as run:
            self.assertEqual(self.run_cli(["--overwrite"]), 1)
        run.assert_not_called()
        self.assertTrue(self.pdf.read_bytes().startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
