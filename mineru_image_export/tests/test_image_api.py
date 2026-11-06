"""模拟解析过程验证图片 ZIP 接口，不启动 MinerU 或调用模型。"""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
import zipfile


HAS_API_DEPS = importlib.util.find_spec("flask") is not None and importlib.util.find_spec("psutil") is not None
if HAS_API_DEPS:
    spec = importlib.util.spec_from_file_location("image_export_api", Path(__file__).resolve().parents[2] / "image_export_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)


@unittest.skipUnless(HAS_API_DEPS, "接口测试需要 Flask 和 psutil，请在 MinerU 环境中运行")
class ImageAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        self.workspaces = []
        original_mkdtemp = api.tempfile.mkdtemp

        def workspace(**kwargs):
            path = original_mkdtemp(prefix="request-", dir=self.root)
            self.workspaces.append(Path(path))
            return path

        patch.object(api.tempfile, "mkdtemp", side_effect=workspace).start()
        patch.object(api, "LOCK_DIR", self.root / "locks").start()
        patch.object(api, "WORKERS", 1).start()
        patch.object(api, "logger").start()
        self.pipeline = patch.object(api, "run_pipeline", side_effect=self.fake_pipeline).start()
        self.client = api.app.test_client()

    def fake_pipeline(self, pdf, metadata, workspace, backend, method, lang):
        records = []
        for index in (1, 1, 2):
            image = workspace / "parsed" / str(index) / "images" / "crop.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(f"image-{index}".encode())
            records.append({"image_path": str(image.relative_to(metadata.parent)),
                            "image_title": f"图 {index}", "section": "2 接口 > 2.1 引脚"})
        metadata.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    def post(self, data=None, **kwargs):
        if data is None:
            data = {"files": (io.BytesIO(b"%PDF-1.7\nfixture"), "example.pdf")}
        return self.client.post("/api/extract-pdf-images-zip", data=data, **kwargs)

    def test_zip_contains_one_json_and_images_with_working_paths(self):
        response = self.post(buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn("example_images.zip", response.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"images/", "example_images.json", "images/crop.png", "images/crop_2.png"})
            records = json.loads(archive.read("example_images.json"))
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["image_path"], records[1]["image_path"])
            for record in records:
                self.assertEqual(set(record), {"image_path", "image_title", "section"})
                self.assertTrue(record["image_path"].startswith("images/"))
                self.assertTrue(archive.read(record["image_path"]).startswith(b"image-"))
        self.assertTrue(all(not p.exists() for p in self.workspaces))
        response.close()

    def test_stream_and_client_close_clean_up_workspace(self):
        response = self.post(buffered=False)
        response.close()
        self.assertTrue(all(not p.exists() for p in self.workspaces))

    def test_file_alias_chinese_filename_and_options(self):
        response = self.post({"file": (io.BytesIO(b"%PDF-1.7"), "器件.pdf"), "backend": "pipeline", "method": "ocr", "lang": "en"}, buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pipeline.call_args.args[-3:], ("pipeline", "ocr", "en"))
        self.assertIn("document_images.zip", response.headers["Content-Disposition"])

    def test_no_files_and_multiple_files_are_rejected(self):
        self.assertEqual(self.post({}).status_code, 400)
        data = {"files": [(io.BytesIO(b"%PDF-1.7"), "a.pdf"), (io.BytesIO(b"%PDF-1.7"), "b.pdf")]}
        self.assertEqual(self.post(data).status_code, 400)
        self.pipeline.assert_not_called()

    def test_bad_extension_invalid_content_and_parameters(self):
        for data in ({"files": (io.BytesIO(b"%PDF-1.7"), "a.txt")},
                     {"files": (io.BytesIO(b"not pdf"), "a.pdf")},
                     {"files": (io.BytesIO(b"%PDF-1.7"), "a.pdf"), "backend": "invalid"}):
            self.assertEqual(self.post(data).status_code, 400)
        self.pipeline.assert_not_called()
        self.assertTrue(all(not p.exists() for p in self.workspaces))

    def test_upload_size_limit_returns_json(self):
        with patch.dict(api.app.config, {"MAX_CONTENT_LENGTH": 20}):
            response = self.post()
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json)
        self.pipeline.assert_not_called()

    def test_pipeline_failure_and_timeout_clean_up(self):
        for error, status in ((subprocess.CalledProcessError(2, ["fake"]), 500),
                              (subprocess.TimeoutExpired(["fake"], 1), 504)):
            self.pipeline.side_effect = error
            response = self.post()
            self.assertEqual(response.status_code, status)
            self.assertTrue(all(not p.exists() for p in self.workspaces))

    def test_missing_or_invalid_json_does_not_return_zip(self):
        for content in (None, "not json", "{}", '[{"image_path": "x"}]'):
            def fake(pdf, metadata, workspace, *args):
                if content is not None: metadata.write_text(content)
            self.pipeline.side_effect = fake
            self.assertEqual(self.post().status_code, 500)
            self.assertTrue(all(not p.exists() for p in self.workspaces))

    def test_empty_image_list_still_returns_json_and_empty_images_directory(self):
        def fake(pdf, metadata, workspace, *args): metadata.write_text("[]")
        self.pipeline.side_effect = fake
        response = self.post(buffered=True)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"images/", "example_images.json"})
            self.assertEqual(json.loads(archive.read("example_images.json")), [])

    def test_workspace_escape_and_non_image_targets_rejected(self):
        secret = self.root / "outside.png"
        secret.write_bytes(b"secret")
        for target in ("../outside.png", "uploaded.pdf"):
            def fake(pdf, metadata, workspace, *args):
                metadata.write_text(json.dumps([{"image_path": target, "image_title": "", "section": ""}]))
            self.pipeline.side_effect = fake
            self.assertEqual(self.post().status_code, 500)

    def test_queue_timeout_and_slot_release(self):
        with api.request_slot(), patch.object(api, "QUEUE_TIMEOUT", 0):
            self.assertEqual(self.post().status_code, 503)
        self.pipeline.assert_not_called()
        response = self.post(buffered=True)
        self.assertEqual(response.status_code, 200)

    def test_two_requests_use_isolated_workspaces(self):
        for _ in range(2): self.assertEqual(self.post(buffered=True).status_code, 200)
        self.assertEqual(len(set(self.workspaces)), 2)
        self.assertTrue(all(not p.exists() for p in self.workspaces))

    def test_health_does_not_start_pipeline(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.pipeline.assert_not_called()

    def test_real_runner_command_uses_independent_tool(self):
        # 原函数已被替换；直接载入独立模块获取未经 mock 的函数。
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        process = MagicMock()
        process.wait.return_value = 0
        with patch.object(fresh.subprocess, "Popen", return_value=process) as popen:
            fresh.run_pipeline(self.root / "a.pdf", self.root / "a.json", self.root, "pipeline", "auto", "en")
        command = popen.call_args.args[0]
        self.assertIn("mineru_image_export/export_images.py", command[1])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_runner_timeout_stops_process_tree(self):
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        process = MagicMock()
        process.wait.side_effect = subprocess.TimeoutExpired(["fake"], 1)
        with patch.object(fresh.subprocess, "Popen", return_value=process), patch.object(fresh, "stop_process_tree") as stop:
            with self.assertRaises(subprocess.TimeoutExpired):
                fresh.run_pipeline(self.root / "a.pdf", self.root / "a.json", self.root, "pipeline", "auto", "en")
        stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
