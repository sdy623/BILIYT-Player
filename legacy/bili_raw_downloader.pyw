#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebView2 desktop shell for the BiliYT original-stream downloader."""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

import pyperclip
import webview

from bili_raw_downloader_core import download_input


APP_TITLE = "Bili RAW 下载器"


def default_download_dir() -> Path:
    override = os.environ.get("BILIYT_DOWNLOAD_DIR")
    if override:
        return Path(override)
    start = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve()
    for parent in start.parents:
        if (parent / "legacy").is_dir() and (parent / "downloads").is_dir():
            return parent / "downloads"
        if parent.name.casefold() == "outputs" and (parent.parent / "downloads").is_dir():
            return parent.parent / "downloads"
    return start.parent / "downloads"


def ui_entrypoint() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "bili_raw_ui" / "index.html"


def app_icon() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "bili_raw_downloader.ico"


def human_size(value: int | None) -> str:
    if value is None:
        return ""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(value)
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TiB"


def progress_payload(message: str, current: int | None, total: int | None) -> dict:
    percent = None
    if current is not None and total:
        percent = round(max(0.0, min(100.0, current * 100.0 / total)), 1)
    return {
        "message": message,
        "current": current,
        "total": total,
        "currentLabel": human_size(current),
        "totalLabel": human_size(total),
        "percent": percent,
        "indeterminate": current is None or not total,
    }


def is_bilibili_input(value: str) -> bool:
    text = value.strip()
    lowered = text.casefold()
    return (
        "bilibili.com" in lowered
        or "b23.tv" in lowered
        or text.startswith(("BV", "av", "ep", "ss", "md"))
    )


class DownloaderApi:
    def __init__(
        self,
        clipboard_reader: Callable[[], str] | None = None,
        download_dir: Path | None = None,
        initial_source: str = "",
        auto_start: bool = False,
    ):
        self._clipboard_reader = clipboard_reader or pyperclip.paste
        self._download_dir = download_dir or default_download_dir()
        self._initial_source = initial_source.strip()
        self._auto_start = auto_start
        self._window = None
        self._running = False
        self._last_message = ""
        self._result_path: Path | None = None
        self._state_lock = threading.Lock()

    def bind_window(self, window) -> None:
        self._window = window

    def get_initial_state(self) -> dict:
        source = self._initial_source
        if not source:
            try:
                candidate = str(self._clipboard_reader() or "").strip()
            except Exception:
                candidate = ""
            if is_bilibili_input(candidate):
                source = candidate
        return {
            "source": source,
            "output": str(self._download_dir),
            "autoStart": self._auto_start and bool(source),
            "runtime": "Microsoft Edge WebView2",
        }

    def choose_output_dir(self, initial: str = "") -> str:
        if self._window is None:
            return ""
        selected = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=initial or str(self._download_dir),
        )
        return str(selected[0]) if selected else ""

    def start_download(self, source: str, output: str) -> dict:
        source = (source or "").strip()
        output = (output or "").strip()
        if not source:
            return {"accepted": False, "error": "请粘贴哔哩哔哩链接或 BV/EP/SS/MD 编号。"}
        if not output:
            return {"accepted": False, "error": "请选择 MKV 保存目录。"}
        with self._state_lock:
            if self._running:
                return {"accepted": False, "error": "当前下载仍在进行。"}
            self._running = True
            self._last_message = ""
            self._result_path = None
        threading.Thread(target=self._worker, args=(source, output), daemon=True).start()
        return {"accepted": True}

    def open_result(self) -> dict:
        with self._state_lock:
            result_path = self._result_path
        if not result_path:
            return {"opened": False, "error": "还没有可打开的成品。"}
        target = result_path if result_path.is_dir() else result_path.parent
        try:
            os.startfile(str(target))
        except OSError as exc:
            return {"opened": False, "error": str(exc)}
        return {"opened": True}

    def _send(self, method: str, payload: dict) -> None:
        if self._window is None:
            return
        encoded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        try:
            self._window.run_js(
                f"window.BiliDownloader && window.BiliDownloader.{method}({encoded});"
            )
        except Exception:
            # Window shutdown must not turn a successful media operation into
            # a background reporting failure.
            pass

    def _report(self, message: str, current: int | None, total: int | None) -> None:
        payload = progress_payload(message, current, total)
        with self._state_lock:
            payload["messageChanged"] = message != self._last_message
            self._last_message = message
        self._send("onReport", payload)

    def _worker(self, source: str, output: str) -> None:
        try:
            result = download_input(source, output, self._report)
        except Exception as exc:
            traceback.print_exc()
            with self._state_lock:
                self._running = False
            self._send("onError", {"error": str(exc)})
            return

        payload = self._result_payload(result)
        with self._state_lock:
            self._running = False
        self._send("onComplete", payload)

    def _result_payload(self, result) -> dict:
        if hasattr(result, "outputs"):
            output_dir = Path(result.output_dir)
            with self._state_lock:
                self._result_path = output_dir
            return {
                "kind": "collection",
                "title": result.title,
                "output": str(output_dir),
                "success": len(result.outputs),
                "failed": len(result.failures),
                "failures": [
                    {"bvid": bvid, "title": title, "error": error}
                    for bvid, title, error in result.failures
                ],
            }

        output_path = Path(result.output)
        with self._state_lock:
            self._result_path = output_path
        tracks = []
        for index, track in enumerate(result.audio_tracks, 1):
            title = (track.get("tags") or {}).get("title", "")
            details = " / ".join(
                str(track.get(key))
                for key in ("codec_name", "sample_rate", "channels", "channel_layout")
                if track.get(key) not in (None, "")
            )
            tracks.append({"index": index, "title": title, "details": details})
        track_count = len(tracks)
        titles = " ".join(track["title"] for track in tracks).casefold()
        if track_count == 2:
            mode = "Hi-Res + Dolby 双音轨"
        elif "hi-res" in titles:
            mode = "Hi-Res 单音轨"
        elif "dolby" in titles:
            mode = "Dolby 单音轨"
        else:
            mode = "普通原始音轨"
        return {
            "kind": "single",
            "output": str(output_path),
            "trackCount": track_count,
            "mode": mode,
            "tracks": tracks,
        }


def command_line_source() -> tuple[str, bool]:
    if len(sys.argv) > 2 and sys.argv[1] == "--start":
        return sys.argv[2], True
    if len(sys.argv) > 1:
        return sys.argv[1], False
    return "", False


def main() -> None:
    initial_source, auto_start = command_line_source()
    api = DownloaderApi(initial_source=initial_source, auto_start=auto_start)
    window = webview.create_window(
        APP_TITLE,
        url=ui_entrypoint().as_uri(),
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 640),
        background_color="#080c10",
        text_select=True,
    )
    api.bind_window(window)
    webview.start(gui="edgechromium", debug=False, private_mode=True, icon=str(app_icon()))


if __name__ == "__main__":
    main()
