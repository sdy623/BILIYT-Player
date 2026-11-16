from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


LEGACY = Path(__file__).resolve().parents[1]
UI = LEGACY / "bili_raw_ui"


def load_launcher():
    legacy_path = str(LEGACY)
    if legacy_path not in sys.path:
        sys.path.insert(0, legacy_path)
    spec = importlib.util.spec_from_file_location(
        "bili_raw_downloader_launcher", LEGACY / "bili_raw_downloader.pyw"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WebViewUiContractTests(unittest.TestCase):
    def test_launcher_uses_edge_webview2_and_per_monitor_assets(self):
        launcher = (LEGACY / "bili_raw_downloader.pyw").read_text(encoding="utf-8")
        spec = (LEGACY / "bili_raw_downloader.spec").read_text(encoding="utf-8")
        self.assertTrue((LEGACY / "bili_raw_downloader.manifest").is_file())
        self.assertTrue((LEGACY / "bili_raw_downloader.ico").is_file())
        manifest = (LEGACY / "bili_raw_downloader.manifest").read_text(encoding="utf-8")

        self.assertIn("import webview", launcher)
        self.assertIn('gui="edgechromium"', launcher)
        self.assertIn("ui_entrypoint().as_uri()", launcher)
        self.assertIn("icon=str(app_icon())", launcher)
        self.assertNotIn("import tkinter", launcher)
        self.assertIn("bili_raw_ui", spec)
        self.assertIn("bili_raw_downloader.manifest", spec)
        self.assertIn("bili_raw_downloader.ico", spec)
        self.assertIn("upx=False", spec)
        self.assertIn("exclude_binaries=True", spec)
        self.assertIn("COLLECT(", spec)
        self.assertIn("PerMonitorV2", manifest)

    def test_frontend_exposes_complete_accessible_download_flow(self):
        self.assertTrue((UI / "index.html").is_file())
        self.assertTrue((UI / "styles.css").is_file())
        self.assertTrue((UI / "app.js").is_file())
        html = (UI / "index.html").read_text(encoding="utf-8")
        css = (UI / "styles.css").read_text(encoding="utf-8")
        script = (UI / "app.js").read_text(encoding="utf-8")

        for element_id in (
            "source-input",
            "output-input",
            "browse-button",
            "download-button",
            "open-button",
            "progress-bar",
            "activity-log",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("window.pywebview.api", script)
        self.assertIn("window.BiliDownloader", script)
        self.assertIn("preventScroll: true", script)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("@media", css)

    def test_frontend_uses_plain_chinese_without_decorative_slogans(self):
        html = (UI / "index.html").read_text(encoding="utf-8")
        script = (UI / "app.js").read_text(encoding="utf-8")
        for slogan in (
            "ORIGINAL STREAM WORKBENCH",
            "STREAM POLICY",
            "SESSION LOG",
            "只收原流，拒绝伪装",
            "01 / SOURCE",
            "02 / ACTIVITY",
        ):
            self.assertNotIn(slogan, html)
        for plain_copy in (
            "粘贴链接，选择目录，然后开始下载",
            "下载内容",
            "下载规则",
            "下载进度",
            "运行记录",
        ):
            self.assertIn(plain_copy, html)
        self.assertIn("开始下载", html)
        self.assertIn("开始下载", script)
        self.assertEqual("Bili RAW 下载器", load_launcher().APP_TITLE)
        self.assertNotIn("Bili " + "MKV", html)

    def test_api_initial_state_prefills_bilibili_clipboard_only(self):
        launcher = load_launcher()
        self.assertTrue(hasattr(launcher, "DownloaderApi"))
        good = launcher.DownloaderApi(
            clipboard_reader=lambda: " https://www.bilibili.com/video/BV1xx411c7mD ",
            download_dir=Path("D:/Media"),
        ).get_initial_state()
        bad = launcher.DownloaderApi(
            clipboard_reader=lambda: "not a video link",
            download_dir=Path("D:/Media"),
        ).get_initial_state()

        self.assertEqual(good["source"], "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(good["output"], "D:\\Media")
        self.assertEqual(bad["source"], "")

    def test_progress_payload_calculates_percentage_and_human_sizes(self):
        launcher = load_launcher()
        self.assertTrue(hasattr(launcher, "progress_payload"))
        payload = launcher.progress_payload("下载视频", 5 * 1024 * 1024, 20 * 1024 * 1024)

        self.assertEqual(payload["percent"], 25.0)
        self.assertEqual(payload["currentLabel"], "5.0 MiB")
        self.assertEqual(payload["totalLabel"], "20.0 MiB")
        self.assertFalse(payload["indeterminate"])


if __name__ == "__main__":
    unittest.main()
