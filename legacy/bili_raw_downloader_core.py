#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download Bilibili's best original video/audio streams and remux to MKV."""

from __future__ import annotations
import concurrent.futures, json, os, re, shutil, subprocess, sys, threading, urllib.error, urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import bili_clipboard_dolby as bili

Report = Callable[[str, int | None, int | None], None]
BV_RE = re.compile(r"(BV[a-zA-Z0-9]{10})")
EP_RE = re.compile(r"(?:^|/)ep(\d+)", re.I)
SS_RE = re.compile(r"(?:^|/)ss(\d+)", re.I)
MD_RE = re.compile(r"(?:^|/)md(\d+)", re.I)
P_RE = re.compile(r"[?&]p=(\d+)", re.I)
SPACE_LIST_RE = re.compile(r"space\.bilibili\.com/(\d+)/lists", re.I)
SID_RE = re.compile(r"[?&]sid=(\d+)", re.I)
BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

@dataclass(frozen=True)
class Stream:
    kind: str
    urls: tuple[str, ...]
    description: str
    codec: str
    bandwidth: int

@dataclass(frozen=True)
class Media:
    bvid: str
    cid: int
    title: str
    video: Stream
    hires: Stream | None
    dolby: Stream | None
    cookie_header: str
    regular: Stream | None = None

    @property
    def audio_streams(self) -> tuple[Stream, ...]:
        """Return only the useful original tracks, in the intended MKV order."""
        special = tuple(x for x in (self.hires, self.dolby) if x is not None)
        if special:
            return special
        return (self.regular,) if self.regular is not None else ()

@dataclass(frozen=True)
class DownloadResult:
    output: Path
    title: str
    video_description: str
    audio_tracks: tuple[dict, ...]
    video_tracks: tuple[dict, ...]


@dataclass(frozen=True)
class Collection:
    mid: str
    sid: str
    title: str
    videos: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BatchDownloadResult:
    output_dir: Path
    title: str
    outputs: tuple[Path, ...]
    failures: tuple[tuple[str, str, str], ...]

def _emit(report: Report | None, message: str,
          current: int | None = None, total: int | None = None) -> None:
    if report:
        report(message, current, total)

def sanitize_filename(value: str, limit: int = 150) -> str:
    value = BAD_FILENAME.sub("＿", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "bilibili-video")[:limit].rstrip(" .")

def _urls(item: dict | None) -> tuple[str, ...]:
    if not item:
        return ()
    values: list[str] = []
    primary = item.get("baseUrl") or item.get("base_url")
    if primary:
        values.append(primary)
    backups = item.get("backupUrl") or item.get("backup_url") or []
    if isinstance(backups, str):
        backups = [backups]
    values.extend(x for x in backups if x)
    return tuple(dict.fromkeys(values))

def _items(value) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []

def _stream(item: dict, kind: str, description: str) -> Stream:
    urls = _urls(item)
    if not urls:
        raise RuntimeError(f"{description} 没有可用 CDN 地址")
    return Stream(kind, urls, description,
                  str(item.get("codecs") or item.get("mimeType") or "?"),
                  int(item.get("bandwidth") or 0))

def select_media_streams(
    dash: dict,
) -> tuple[Stream, Stream | None, Stream | None, Stream | None]:
    """Select best video plus every non-redundant premium audio track.

    Hi-Res and Dolby are kept together when both exist.  If neither premium
    track exists, the best regular DASH audio is used instead.
    """
    dolby_block = dash.get("dolby") or {}
    candidates = [
        (item, is_dv)
        for item, is_dv in (
            [(x, False) for x in _items(dash.get("video"))]
            + [(x, True) for x in _items(dolby_block.get("video"))]
        )
        if _urls(item)
    ]
    if not candidates:
        raise RuntimeError("接口没有返回可用的视频 DASH 原流")
    best_video, is_dv = max(
        candidates,
        key=lambda pair: (
            int(pair[0].get("id") or 0),
            int(pair[0].get("bandwidth") or 0),
            1 if pair[1] else 0,
            bili.CODEC_WEIGHT.get(pair[0].get("codecid"), 0),
        ),
    )
    width, height = best_video.get("width") or "?", best_video.get("height") or "?"
    fps = best_video.get("frameRate") or best_video.get("frame_rate") or "?"
    codec = best_video.get("codecs") or "?"
    dv = " Dolby Vision" if is_dv else ""
    video = _stream(
        best_video, "video",
        f"{width}×{height} {fps}fps {codec}{dv} · {int(best_video.get('bandwidth') or 0) / 1_000_000:.2f} Mb/s",
    )

    dolby_items = [x for x in _items(dolby_block.get("audio")) if _urls(x)]
    dolby = None
    if dolby_items:
        best_dolby = max(dolby_items, key=lambda x: int(x.get("bandwidth") or 0))
        dolby = _stream(
            best_dolby, "dolby",
            f"Dolby Atmos / E-AC-3 · {int(best_dolby.get('bandwidth') or 0) / 1000:.0f} kb/s",
        )

    hires_items = [x for x in _items((dash.get("flac") or {}).get("audio")) if _urls(x)]
    hires = None
    if hires_items:
        best_hires = max(hires_items, key=lambda x: int(x.get("bandwidth") or 0))
        hires = _stream(
            best_hires, "hires",
            f"Hi-Res Lossless / FLAC · {int(best_hires.get('bandwidth') or 0) / 1000:.0f} kb/s",
        )
        if dolby and set(dolby.urls) == set(hires.urls):
            raise RuntimeError("接口把 Hi-Res 与 Dolby 返回成同一地址，已停止以免生成伪双音轨")

    regular = None
    if not hires and not dolby:
        regular_items = [x for x in _items(dash.get("audio")) if _urls(x)]
        if not regular_items:
            raise RuntimeError("接口没有返回可用的音频 DASH 原流")
        best_regular = max(regular_items, key=lambda x: int(x.get("bandwidth") or 0))
        regular_codec = str(best_regular.get("codecs") or best_regular.get("mimeType") or "audio")
        regular = _stream(
            best_regular, "regular",
            f"普通原始音频 / {regular_codec} · {int(best_regular.get('bandwidth') or 0) / 1000:.0f} kb/s",
        )
    return video, hires, dolby, regular

def _resolve_short_url(text: str) -> str:
    stripped = text.strip()
    if not re.search(r"https?://(?:www\.)?b23\.tv/", stripped, re.I):
        return stripped
    request = urllib.request.Request(stripped, headers=bili.COMMON_HEADERS)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        return response.geturl()

def parse_collection_reference(text: str) -> tuple[str, str] | None:
    mid_match = SPACE_LIST_RE.search(text.strip())
    sid_match = SID_RE.search(text.strip())
    if not (mid_match and sid_match):
        return None
    return mid_match.group(1), sid_match.group(1)


def fetch_collection(text: str, report: Report | None = None) -> Collection:
    reference = parse_collection_reference(text)
    if not reference:
        raise ValueError("没有识别到空间合集链接和 sid")
    mid, sid = reference
    _emit(report, "正在读取合集目录…")
    sessdata, source = bili.import_browser_sessdata()
    if not sessdata:
        raise RuntimeError("Edge / Firefox 中没有找到有效的 B站登录。请先登录后重试。")
    session, _, _ = bili.init_bili_session(sessdata)
    videos: list[tuple[str, str]] = []
    title = f"B站合集 {sid}"
    page_num = 1
    total = None
    while total is None or len(videos) < total:
        response = session.get(
            "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
            params={
                "mid": mid, "season_id": sid, "sort_reverse": "false",
                "page_num": page_num, "page_size": 30,
            },
            headers=bili.COMMON_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"合集目录读取失败：{payload.get('message')}")
        data = payload.get("data") or {}
        meta = data.get("meta") or {}
        title = meta.get("title") or meta.get("name") or title
        total = int((data.get("page") or {}).get("total") or meta.get("total") or 0)
        page_items = data.get("archives") or []
        for item in page_items:
            bvid = item.get("bvid")
            if bvid:
                videos.append((bvid, item.get("title") or bvid))
        if not page_items or len(videos) >= total:
            break
        page_num += 1
    if not videos:
        raise RuntimeError("该合集没有找到可下载的视频")
    _emit(report, f"合集：{title}，共 {len(videos)} 个视频")
    return Collection(mid, sid, title, tuple(videos))


def parse_reference(text: str) -> tuple[str, str, int]:
    text = _resolve_short_url(text)
    p_match = P_RE.search(text)
    page = max(1, int(p_match.group(1))) if p_match else 1
    for kind, pattern in (("ep", EP_RE), ("ss", SS_RE), ("md", MD_RE), ("bv", BV_RE)):
        match = pattern.search(text)
        if match:
            return kind, match.group(1), page
    raise ValueError("没有识别到 BV、EP、SS 或 MD 链接/编号")

def _cookie_header(session) -> str:
    values = getattr(session, "_cookies", {})
    return "; ".join(f"{key}={value}" for key, value in values.items() if value)

def resolve_media(text: str, report: Report | None = None) -> Media:
    _emit(report, "正在从 Edge / Firefox 读取你的 B站登录状态…")
    sessdata, source = bili.import_browser_sessdata()
    if not sessdata:
        raise RuntimeError("Edge / Firefox 中没有找到有效的 B站登录。请先登录 bilibili 后重试。")
    _emit(report, f"已直接登录：{source}；正在读取最高质量原流…")
    session, img_key, sub_key = bili.init_bili_session(sessdata)
    kind, identifier, page = parse_reference(text)
    if kind == "bv":
        bvid = identifier
        cid, title = bili.get_cid_and_title(session, bvid, page)
        result = bili.get_playurl(session, bvid, cid, img_key, sub_key)
    elif kind == "ep":
        bvid, cid, title = bili.resolve_episode(session, int(identifier))
        result = bili.get_playurl_pgc(session, bvid, cid)
    elif kind == "ss":
        bvid, cid, title = bili.resolve_ss(session, int(identifier))
        result = bili.get_playurl_pgc(session, bvid, cid)
    else:
        bvid, cid, title = bili.resolve_md(session, int(identifier))
        result = bili.get_playurl_pgc(session, bvid, cid)
    dash = result.get("dash")
    if not dash:
        raise RuntimeError("该链接只返回合并流，没有可分别保存的最高画质 DASH 原流")
    video, hires, dolby, regular = select_media_streams(dash)
    _emit(report, f"视频：{video.description}")
    selected = tuple(x for x in (hires, dolby) if x is not None) or (regular,)
    if hires and dolby:
        _emit(report, f"音轨 1：{hires.description}")
        _emit(report, f"音轨 2：{dolby.description}（默认播放）")
    else:
        assert selected[0] is not None
        _emit(report, f"音轨 1：{selected[0].description}（默认播放）")
        if hires:
            _emit(report, "此视频只有 Hi-Res；保留 FLAC 单音轨")
        elif dolby:
            _emit(report, "此视频只有 Dolby；保留 Atmos 单音轨")
        else:
            _emit(report, "此视频没有 Hi-Res / Dolby 音频；保留最高码率普通原始音轨")
    return Media(
        bvid, int(cid), title, video, hires, dolby, _cookie_header(session), regular,
    )

def _total(headers, existing: int, status: int) -> int | None:
    match = re.search(r"/(\d+)$", headers.get("Content-Range", ""))
    if match:
        return int(match.group(1))
    length = headers.get("Content-Length")
    if length and length.isdigit():
        return existing + int(length) if status == 206 else int(length)
    return None

def _download_url(url: str, destination: Path, cookie_header: str,
                  label: str, report: Report | None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.stat().st_size if destination.exists() else 0
    headers = dict(bili.COMMON_HEADERS)
    headers.update({"Accept": "*/*", "Accept-Encoding": "identity"})
    if cookie_header:
        headers["Cookie"] = cookie_header
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=45)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing:
            _emit(report, f"{label} 已下载完成，继续封装")
            return destination
        raise
    with response:
        status = int(getattr(response, "status", response.getcode()))
        if existing and status != 206:
            response.close()
            destination.unlink(missing_ok=True)
            return _download_url(url, destination, cookie_header, label, report)
        total = _total(response.headers, existing, status)
        mode = "ab" if existing and status == 206 else "wb"
        downloaded = existing if mode == "ab" else 0
        _emit(report, f"下载 {label}", downloaded, total)
        with destination.open(mode) as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
                downloaded += len(block)
                _emit(report, f"下载 {label}", downloaded, total)
    if total is not None and destination.stat().st_size != total:
        raise RuntimeError(f"{label} 下载不完整：{destination.stat().st_size} / {total} bytes")
    return destination


def _download_url_resumable(url: str, destination: Path, cookie_header: str,
                            label: str, report: Report | None,
                            attempts: int = 6) -> Path:
    """Retry an interrupted OpenSSL transfer and resume it with HTTP Range."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _download_url(url, destination, cookie_header, label, report)
        except urllib.error.HTTPError:
            # A CDN policy response such as Bilibili's 567 will not improve by
            # retrying the same signed URL. Move to the backup URL immediately.
            raise
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            existing = destination.stat().st_size if destination.exists() else 0
            suffix = f"（已保留 {existing / 1048576:.1f} MiB）" if existing else ""
            _emit(report, f"连接中断，正在断点续传 {label}：第 {attempt + 1}/{attempts} 次{suffix}")
    assert last_error is not None
    raise last_error


def _split_ranges(start: int, stop: int, connections: int) -> list[tuple[int, int]]:
    """Split the half-open byte interval [start, stop) into contiguous ranges."""
    length = max(0, stop - start)
    if not length:
        return []
    count = max(1, min(connections, length))
    base, extra = divmod(length, count)
    ranges: list[tuple[int, int]] = []
    cursor = start
    for index in range(count):
        size = base + (1 if index < extra else 0)
        ranges.append((cursor, cursor + size - 1))
        cursor += size
    return ranges


def _request_headers(cookie_header: str, byte_range: str | None = None) -> dict[str, str]:
    headers = dict(bili.COMMON_HEADERS)
    headers.update({"Accept": "*/*", "Accept-Encoding": "identity"})
    if cookie_header:
        headers["Cookie"] = cookie_header
    if byte_range:
        headers["Range"] = byte_range
    return headers


def _probe_url_size(url: str, cookie_header: str) -> int:
    request = urllib.request.Request(
        url, headers=_request_headers(cookie_header, "bytes=0-0"),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=45) as response:
        status = int(getattr(response, "status", response.getcode()))
        content_range = response.headers.get("Content-Range", "")
        match = re.search(r"/(\d+)$", content_range)
        if status != 206 or not match:
            raise RuntimeError("CDN 不支持 OpenSSL Range 多连接")
        response.read(1)
        return int(match.group(1))


def _download_range_part(url: str, cookie_header: str, start: int, end: int,
                         part: Path, progress: Callable[[int], None],
                         attempts: int = 6) -> Path:
    expected = end - start + 1
    if part.exists() and part.stat().st_size > expected:
        with part.open("r+b") as handle:
            handle.truncate(expected)
    for attempt in range(1, attempts + 1):
        existing = part.stat().st_size if part.exists() else 0
        if existing == expected:
            return part
        request_start = start + existing
        request = urllib.request.Request(
            url,
            headers=_request_headers(cookie_header, f"bytes={request_start}-{end}"),
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=45) as response:
                status = int(getattr(response, "status", response.getcode()))
                if status != 206:
                    raise RuntimeError(f"Range 返回状态 {status}")
                with part.open("ab") as handle:
                    while part.stat().st_size < expected:
                        remaining = expected - part.stat().st_size
                        block = response.read(min(1024 * 1024, remaining))
                        if not block:
                            break
                        handle.write(block)
                        progress(len(block))
            if part.stat().st_size == expected:
                return part
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == attempts:
                raise
    actual = part.stat().st_size if part.exists() else 0
    raise RuntimeError(f"分片下载不完整：{actual} / {expected} bytes")


def _download_url_parallel(url: str, destination: Path, cookie_header: str,
                           label: str, report: Report | None,
                           connections: int = 8) -> Path:
    """Use Python/OpenSSL Range requests in parallel, avoiding aria2/SChannel."""
    total = _probe_url_size(url, cookie_header)
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.stat().st_size if destination.exists() else 0
    if existing > total:
        destination.unlink(missing_ok=True)
        existing = 0
    if existing == total:
        _emit(report, f"{label} 已下载完成，继续封装", 100, 100)
        return destination

    remaining = total - existing
    count = max(1, min(connections, (remaining + 16 * 1024 * 1024 - 1) // (16 * 1024 * 1024)))
    ranges = _split_ranges(existing, total, count)
    parts = [
        destination.with_name(
            f"{destination.name}.openssl-part-{i:02d}-{start}-{end}"
        )
        for i, (start, end) in enumerate(ranges)
    ]
    completed = existing + sum(
        min(part.stat().st_size, end - start + 1) if part.exists() else 0
        for part, (start, end) in zip(parts, ranges)
    )
    lock = threading.Lock()

    def progress(amount: int) -> None:
        nonlocal completed
        with lock:
            completed += amount
            _emit(report, f"Python/OpenSSL {count} 连接下载 {label}", completed, total)

    _emit(report, f"Python/OpenSSL {count} 连接下载 {label}", completed, total)
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(
                _download_range_part, url, cookie_header, start, end, part, progress,
            )
            for part, (start, end) in zip(parts, ranges)
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    with destination.open("ab") as output:
        for part, (start, end) in zip(parts, ranges):
            expected = end - start + 1
            if part.stat().st_size != expected:
                raise RuntimeError(f"分片合并前长度错误：{part.name}")
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
            part.unlink()
    if destination.stat().st_size != total:
        raise RuntimeError(f"{label} 多连接下载不完整：{destination.stat().st_size} / {total} bytes")
    _emit(report, f"{label} 下载完成", total, total)
    return destination

def _aria2_command(aria2: str, url: str, destination: Path,
                   cookie_header: str, connections: int = 16) -> list[str]:
    command = [
        aria2,
        "--continue=true",
        f"--max-connection-per-server={connections}",
        f"--split={connections}",
        "--min-split-size=1M",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--max-tries=5",
        "--retry-wait=1",
        "--connect-timeout=15",
        "--timeout=45",
        "--console-log-level=warn",
        "--summary-interval=1",
        "--download-result=hide",
        "--show-console-readout=true",
        f"--dir={destination.parent}",
        f"--out={destination.name}",
        f"--header=User-Agent: {bili.UA}",
        f"--header=Referer: {bili.REFERER}",
        "--header=Origin: https://www.bilibili.com",
    ]
    if cookie_header:
        command.append(f"--header=Cookie: {cookie_header}")
    command.append(url)
    return command


def _parse_aria2_progress(line: str) -> tuple[int, str] | None:
    match = re.search(r"\((\d{1,3})%\).*?\bDL:([^\s\]]+)", line)
    if not match:
        return None
    return min(100, int(match.group(1))), match.group(2)


def _wget2_command(wget2: str, url: str, destination: Path,
                   cookie_header: str, connections: int = 8) -> list[str]:
    """Build a resumable GNU Wget2 chunked-download command."""
    command = [
        wget2,
        "--continue",
        "--chunk-size=1M",
        f"--max-threads={connections}",
        "--tries=5",
        "--timeout=45",
        "--progress=bar",
        f"--header=User-Agent: {bili.UA}",
        f"--referer={bili.REFERER}",
        "--header=Origin: https://www.bilibili.com",
    ]
    if cookie_header:
        command.append(f"--header=Cookie: {cookie_header}")
    command += [f"--output-document={destination}", url]
    return command


def _parse_wget2_progress(line: str) -> tuple[int, str | None] | None:
    match = re.search(r"(?:^|\s)(\d{1,3})%", line)
    if not match:
        return None
    speed_match = re.search(r"([\d.]+\s*[KMGT]?i?B/s)", line, re.I)
    return min(100, int(match.group(1))), speed_match.group(1) if speed_match else None


def _download_aria2(aria2: str, url: str, destination: Path,
                    cookie_header: str, label: str,
                    report: Report | None, connections: int = 16) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.stat().st_size if destination.exists() else 0
    suffix = f"（从 {existing / 1048576:.1f} MiB 续传）" if existing else ""
    _emit(report, f"aria2 {connections} 连接下载 {label}{suffix}")
    process = subprocess.Popen(
        _aria2_command(aria2, url, destination, cookie_header, connections),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_silent_startup(),
    )
    recent: list[str] = []
    buffer = ""
    assert process.stdout is not None
    while True:
        char = process.stdout.read(1)
        if char == "":
            if process.poll() is not None:
                break
            continue
        if char in "\r\n":
            line = buffer.strip()
            buffer = ""
            if not line:
                continue
            recent.append(line)
            del recent[:-30]
            parsed = _parse_aria2_progress(line)
            if parsed:
                percent, speed = parsed
                _emit(report, f"aria2 {connections} 连接 · {label} · {percent}% · {speed}/s",
                      percent, 100)
        else:
            buffer += char
    returncode = process.wait()
    if returncode:
        tail = "\n".join(recent)[-3000:]
        if cookie_header:
            tail = tail.replace(cookie_header, "<Cookie 已隐藏>")
        tail = re.sub(r"\x1b\[[0-9;]*m", "", tail)
        tail = re.sub(r"URI=\S+", "URI=<CDN URL 已隐藏>", tail)
        raise RuntimeError(f"aria2 返回 {returncode}: {tail}")
    control = destination.with_name(destination.name + ".aria2")
    control.unlink(missing_ok=True)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("aria2 没有生成有效文件")
    _emit(report, f"{label} 下载完成", 100, 100)
    return destination


def _download_wget2(wget2: str, url: str, destination: Path,
                    cookie_header: str, label: str,
                    report: Report | None, connections: int = 8) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = destination.stat().st_size if destination.exists() else 0
    suffix = f"（从 {existing / 1048576:.1f} MiB 续传）" if existing else ""
    _emit(report, f"GNU Wget2 {connections} 线程下载 {label}{suffix}")
    process = subprocess.Popen(
        _wget2_command(wget2, url, destination, cookie_header, connections),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_silent_startup(),
    )
    recent: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.strip()
        if not line:
            continue
        recent.append(line)
        del recent[:-30]
        parsed = _parse_wget2_progress(line)
        if parsed:
            percent, speed = parsed
            speed_text = f" · {speed}" if speed else ""
            _emit(report, f"GNU Wget2 {connections} 线程 · {label} · {percent}%{speed_text}",
                  percent, 100)
    returncode = process.wait()
    if returncode:
        tail = "\n".join(recent)[-3000:]
        if cookie_header:
            tail = tail.replace(cookie_header, "<Cookie 已隐藏>")
        tail = re.sub(r"https?://\S+", "<CDN URL 已隐藏>", tail)
        raise RuntimeError(f"GNU Wget2 返回 {returncode}: {tail}")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("GNU Wget2 没有生成有效文件")
    _emit(report, f"{label} 下载完成", 100, 100)
    return destination


def download_stream(stream: Stream, destination: Path, cookie_header: str,
                    report: Report | None = None) -> Path:
    control = destination.with_name(destination.name + ".aria2")
    # A file without aria2's control sidecar was written sequentially by the
    # OpenSSL fallback, so every existing byte is a valid contiguous prefix.
    # Do not feed it back to aria2 (which would create split state and force us
    # to discard the already verified prefix after another SChannel failure).
    openssl_prefix = destination.exists() and not control.exists()
    try:
        aria2 = locate_tool("aria2c")
    except RuntimeError:
        aria2 = None
    try:
        wget2 = locate_tool("wget2")
    except RuntimeError:
        wget2 = None
    if not aria2 and not wget2:
        _emit(report, "未找到 aria2c / GNU Wget2，使用内置 Python/OpenSSL 多连接下载")

    errors: list[str] = []
    if aria2 and not openssl_prefix:
        # B站部分 CDN 会拒绝 16 连接（567），Akamai 在 Windows aria2/SChannel
        # 偶发 80090301。先保留 .aria2 分片逐级降连接续传。
        for connections in (16, 4, 1):
            for index, url in enumerate(stream.urls, 1):
                try:
                    return _download_aria2(
                        aria2, url, destination, cookie_header,
                        stream.description, report, connections,
                    )
                except Exception as exc:
                    errors.append(f"aria2 {connections}连接 / CDN {index}: {exc}")
                    _emit(
                        report,
                        f"{stream.kind} 的 CDN {index} 拒绝 {connections} 连接，正在自动降级…",
                    )

        # aria2 的多分片文件可能已预分配到完整尺寸，不能交给普通 Range
        # 续传（会误判完成）。所有 aria2 方案均失败时，清掉此流的临时
        # 分片与控制文件，改用 Python/OpenSSL 从零重下；其它视频不受影响。
        control.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        _emit(report, f"aria2/CDN 不兼容，改用 GNU Wget2 或 Python/OpenSSL 下载 {stream.description}")
    elif openssl_prefix:
        existing = destination.stat().st_size
        _emit(
            report,
            f"检测到 OpenSSL 连续分片，从 {existing / 1048576:.1f} MiB 直接多连接续传",
        )

    if wget2 and not openssl_prefix:
        for connections in (8, 4, 1):
            for index, url in enumerate(stream.urls, 1):
                try:
                    return _download_wget2(
                        wget2, url, destination, cookie_header,
                        stream.description, report, connections,
                    )
                except Exception as exc:
                    errors.append(f"GNU Wget2 {connections}线程 / CDN {index}: {exc}")
                    _emit(
                        report,
                        f"GNU Wget2 的 CDN {index} 拒绝 {connections} 线程，正在自动降级…",
                    )
        _emit(report, f"GNU Wget2/CDN 不兼容，改用内置 OpenSSL 下载 {stream.description}")

    # aria2 on Windows uses SChannel and can fail with 80090301.  Python uses
    # OpenSSL, so retain parallel Range downloads before falling back to one
    # resumable connection.
    for index, url in enumerate(stream.urls, 1):
        try:
            return _download_url_parallel(
                url, destination, cookie_header, stream.description, report,
            )
        except Exception as exc:
            errors.append(f"OpenSSL 多连接 / CDN {index}: {exc}")
            _emit(report, f"OpenSSL 多连接的 CDN {index} 失败，正在换备用节点…")

    for index, url in enumerate(stream.urls, 1):
        try:
            return _download_url_resumable(
                url, destination, cookie_header, stream.description, report,
            )
        except Exception as exc:
            errors.append(f"OpenSSL / CDN {index}: {exc}")
            _emit(report, f"OpenSSL 的 CDN {index} 失败，正在换备用节点…")
    safe = "；".join(errors[-8:])
    safe = re.sub(r"https?://\S+", "<CDN URL 已隐藏>", safe)
    raise RuntimeError(f"{stream.description} 下载失败；{safe}")


def locate_tool(name: str) -> str:
    override = os.environ.get(f"BILIYT_{name.upper()}")
    if override and Path(override).is_file():
        return override
    roots = [Path(sys.executable).resolve().parent, Path(__file__).resolve().parent]
    if getattr(sys, "_MEIPASS", None):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        candidate = root / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    winget_link = (Path(os.environ.get("LOCALAPPDATA", ""))
                   / "Microsoft" / "WinGet" / "Links" / f"{name}.exe")
    if winget_link.is_file():
        return str(winget_link)
    if name.casefold() == "mkvmerge":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            program_files = Path(os.environ.get(env_name, ""))
            candidate = program_files / "MKVToolNix" / "mkvmerge.exe"
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"未找到 {name}.exe。请安装 FFmpeg，或把 {name}.exe 放到下载器旁边。")

def _silent_startup() -> dict:
    if sys.platform != "win32":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}

def _probe_video_codec_tag(video: Path) -> str:
    command = [
        locate_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_tag_string", "-of", "default=nw=1:nk=1",
        str(video),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", **_silent_startup())
    if completed.returncode:
        raise RuntimeError(f"FFprobe 无法识别视频封装：{completed.stderr[-1000:]}")
    return (completed.stdout or "").strip().casefold()


def _audio_track_title(stream: Stream) -> str:
    if stream.kind == "hires":
        return "Hi-Res Lossless (FLAC)"
    if stream.kind == "dolby":
        return "Dolby Atmos (E-AC-3 JOC)"
    return f"Bilibili Original Audio ({stream.codec})"


def _default_audio_index(audio_inputs: tuple[tuple[Path, Stream], ...]) -> int:
    for index, (_path, stream) in enumerate(audio_inputs):
        if stream.kind == "dolby":
            return index
    return 0


def _mux_with_mkvmerge(video: Path,
                       audio_inputs: tuple[tuple[Path, Stream], ...],
                       temp_output: Path, media: Media) -> None:
    """用 MKVToolNix 复制封装 Dolby Vision；不解码、不转码。"""
    command = [locate_tool("mkvmerge"), "-o", str(temp_output),
               "--title", media.title]
    common_omit = ["--no-subtitles", "--no-buttons", "--no-chapters",
                   "--no-track-tags", "--no-global-tags"]
    command += [
        "--no-audio", *common_omit,
        "--track-name", f"0:{media.video.description}", str(video),
    ]
    default_index = _default_audio_index(audio_inputs)
    for index, (audio_path, stream) in enumerate(audio_inputs):
        command += [
            "--no-video", *common_omit,
            "--track-name", f"0:{_audio_track_title(stream)}",
            "--language", "0:und",
            "--default-track-flag", f"0:{'yes' if index == default_index else 'no'}",
            str(audio_path),
        ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", **_silent_startup())
    # MKVToolNix: 0=成功，1=成功但有警告，2=失败。
    if completed.returncode >= 2:
        tail = (completed.stderr or completed.stdout or "")[-3000:]
        raise RuntimeError(f"MKVToolNix 封装失败：\n{tail}")


def _mux_with_ffmpeg(video: Path,
                     audio_inputs: tuple[tuple[Path, Stream], ...],
                     temp_output: Path, media: Media) -> None:
    command = [
        locate_tool("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(video),
    ]
    for audio_path, _stream in audio_inputs:
        command += ["-i", str(audio_path)]
    command += ["-map", "0:v:0"]
    for index in range(len(audio_inputs)):
        command += ["-map", f"{index + 1}:a:0"]
    command += [
        "-map_metadata", "-1", "-c", "copy",
        "-metadata", f"title={media.title}",
        "-metadata", f"comment=Bilibili original DASH remux; {media.bvid}; cid={media.cid}; no transcoding",
        "-metadata:s:v:0", f"title={media.video.description}",
    ]
    default_index = _default_audio_index(audio_inputs)
    for index, (_audio_path, stream) in enumerate(audio_inputs):
        command += [
            f"-metadata:s:a:{index}", f"title={_audio_track_title(stream)}",
            f"-metadata:s:a:{index}", "language=und",
            f"-disposition:a:{index}", "default" if index == default_index else "0",
        ]
    command.append(str(temp_output))
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", **_silent_startup())
    if completed.returncode:
        tail = (completed.stderr or completed.stdout or "")[-3000:]
        raise RuntimeError(f"FFmpeg 封装失败：\n{tail}")


def mux_mkv(video: Path, audio_inputs: tuple[tuple[Path, Stream], ...],
            output: Path, media: Media, report: Report | None = None) -> None:
    if not audio_inputs:
        raise RuntimeError("没有可封装的音频原流")
    temp_output = output.with_suffix(".muxing.mkv")
    temp_output.unlink(missing_ok=True)
    mode = " + ".join(
        {"hires": "Hi-Res", "dolby": "Dolby", "regular": "普通音频"}.get(stream.kind, stream.kind)
        for _path, stream in audio_inputs
    )
    mode += " 双音轨" if len(audio_inputs) > 1 else " 单音轨"
    _emit(report, f"正在无损封装 MKV（{mode}，不转码）…")
    codec_tag = _probe_video_codec_tag(video)
    if codec_tag in {"dvh1", "dvhe"}:
        _emit(report, "检测到 Dolby Vision MP4 标记，改用 MKVToolNix 保留 DV 配置与 RPU…")
        try:
            _mux_with_mkvmerge(video, audio_inputs, temp_output, media)
        except RuntimeError as exc:
            if "未找到 mkvmerge.exe" in str(exc):
                raise RuntimeError(
                    "Dolby Vision dvh1/dvhe 无法由 FFmpeg 直接写入 MKV；"
                    "请安装 MKVToolNix，或把 mkvmerge.exe 放到下载器旁边。"
                ) from exc
            raise
    else:
        _mux_with_ffmpeg(video, audio_inputs, temp_output, media)
    os.replace(temp_output, output)


def probe_mkv(
    path: Path,
    expected_audio_kinds: tuple[str, ...] | None = None,
) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    command = [
        locate_tool("ffprobe"), "-v", "error", "-show_entries",
        "stream=index,codec_type,codec_name,profile,channels,channel_layout,sample_rate,width,height:stream_tags=title,language",
        "-of", "json", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", **_silent_startup())
    if completed.returncode:
        raise RuntimeError(f"FFprobe 验证失败：{completed.stderr[-2000:]}")
    streams = json.loads(completed.stdout).get("streams") or []
    video = tuple(x for x in streams if x.get("codec_type") == "video")
    audio = tuple(x for x in streams if x.get("codec_type") == "audio")
    if not video or not audio:
        raise RuntimeError(f"成品验证失败：视频轨 {len(video)}，音轨 {len(audio)}")
    if expected_audio_kinds is not None and len(audio) != len(expected_audio_kinds):
        raise RuntimeError(
            f"成品音轨数不符：期望 {len(expected_audio_kinds)}，实际 {len(audio)}"
        )
    if expected_audio_kinds is None and len(audio) not in (1, 2):
        raise RuntimeError(f"成品音轨数异常：{len(audio)}")
    for index, kind in enumerate(expected_audio_kinds or ()):
        codec = str(audio[index].get("codec_name") or "").lower()
        if kind == "hires" and codec != "flac":
            raise RuntimeError(f"第 {index + 1} 音轨不是要求的 FLAC Hi-Res：{codec}")
        if kind == "dolby" and codec not in {"eac3", "ac3"}:
            raise RuntimeError(f"第 {index + 1} 音轨不是要求的 Dolby E-AC-3/AC-3：{codec}")
    return video, audio


def _available_output(path: Path, expected_audio_kinds: tuple[str, ...]) -> Path:
    if not path.exists():
        return path
    try:
        probe_mkv(path, expected_audio_kinds=expected_audio_kinds)
        return path
    except Exception:
        pass
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("输出目录中同名文件过多")


def download_original_mkv(text: str, output_dir: str | Path,
                          report: Report | None = None,
                          filename_prefix: str = "") -> DownloadResult:
    media = resolve_media(text, report)
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(
        f"{filename_prefix}{media.title} [{media.bvid}-{media.cid}]"
    )
    audio_streams = media.audio_streams
    if not audio_streams:
        raise RuntimeError("没有选出可用音频原流")
    audio_kinds = tuple(stream.kind for stream in audio_streams)
    final = _available_output(output_dir / f"{base}.mkv", audio_kinds)
    if final.exists():
        video_tracks, audio_tracks = probe_mkv(final, expected_audio_kinds=audio_kinds)
        mode = " + ".join(audio_kinds)
        _emit(report, f"已存在验证通过的{mode} MKV，直接使用。")
        return DownloadResult(final, media.title, media.video.description, audio_tracks, video_tracks)
    work = output_dir / ".bili-raw-work" / f"{media.bvid}-{media.cid}"
    work.mkdir(parents=True, exist_ok=True)
    video_file = download_stream(media.video, work / "video.m4s", media.cookie_header, report)
    audio_inputs: list[tuple[Path, Stream]] = []
    for stream in audio_streams:
        audio_path = download_stream(
            stream, work / f"audio-{stream.kind}.m4s", media.cookie_header, report,
        )
        audio_inputs.append((audio_path, stream))
    mux_mkv(video_file, tuple(audio_inputs), final, media, report)
    expected = " + ".join(audio_kinds)
    _emit(report, f"正在核对成品的{expected}…")
    video_tracks, audio_tracks = probe_mkv(final, expected_audio_kinds=audio_kinds)
    shutil.rmtree(work, ignore_errors=True)
    _emit(report, f"完成：{final}")
    return DownloadResult(final, media.title, media.video.description, audio_tracks, video_tracks)


def download_dual_audio_mkv(text: str, output_dir: str | Path,
                            report: Report | None = None,
                            filename_prefix: str = "") -> DownloadResult:
    """Compatibility entry point retained for existing callers."""
    return download_original_mkv(text, output_dir, report, filename_prefix)


def download_input(text: str, output_dir: str | Path,
                   report: Report | None = None) -> DownloadResult | BatchDownloadResult:
    if not parse_collection_reference(text):
        return download_original_mkv(text, output_dir, report)

    collection = fetch_collection(text, report)
    root = Path(output_dir).expanduser().resolve()
    folder = root / sanitize_filename(
        f"合集 - {collection.title} [sid-{collection.sid}]"
    )
    folder.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    failures: list[tuple[str, str, str]] = []
    count = len(collection.videos)
    for index, (bvid, title) in enumerate(collection.videos, 1):
        prefix = f"[{index}/{count}]"
        _emit(report, f"{prefix} 开始：{title}")

        def item_report(message: str, current: int | None, total: int | None,
                        item_prefix: str = prefix) -> None:
            _emit(report, f"{item_prefix} {message}", current, total)

        try:
            result = download_original_mkv(
                bvid, folder, item_report, filename_prefix=f"{index:02d}. "
            )
            outputs.append(result.output)
        except Exception as exc:
            failures.append((bvid, title, str(exc)))
            _emit(report, f"{prefix} 失败，继续下一个：{exc}")

    report_path = folder / "批量下载结果.txt"
    lines = [
        f"合集：{collection.title}",
        f"成功：{len(outputs)} / {count}",
        "",
        "成功文件：",
        *[str(path.name) for path in outputs],
    ]
    if failures:
        lines += ["", "失败项目："]
        lines += [f"{bvid} | {title} | {error}" for bvid, title, error in failures]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not outputs:
        raise RuntimeError(
            f"合集 {count} 个视频均未完成；详情见 {report_path}"
        )
    _emit(report, f"合集完成：成功 {len(outputs)}/{count}，失败 {len(failures)}")
    return BatchDownloadResult(
        folder, collection.title, tuple(outputs), tuple(failures)
    )
