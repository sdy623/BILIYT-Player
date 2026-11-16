"""Tests for the original-stream downloader."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bili_raw_downloader_core as core


def item(url, *, stream_id=80, bandwidth=1_000_000, codecs="hev1", codecid=12,
         width=1920, height=1080):
    return {
        "baseUrl": url,
        "id": stream_id,
        "bandwidth": bandwidth,
        "codecs": codecs,
        "codecid": codecid,
        "width": width,
        "height": height,
        "frameRate": "60",
    }


class ParseTests(unittest.TestCase):
    def test_bv_and_page(self):
        self.assertEqual(
            core.parse_reference("https://www.bilibili.com/video/BV1xx411c7mD?p=3"),
            ("bv", "BV1xx411c7mD", 3),
        )

    def test_episode_ids(self):
        self.assertEqual(core.parse_reference("ep123"), ("ep", "123", 1))
        self.assertEqual(core.parse_reference("https://www.bilibili.com/bangumi/play/ss456"),
                         ("ss", "456", 1))
        self.assertEqual(core.parse_reference("https://www.bilibili.com/bangumi/media/md789"),
                         ("md", "789", 1))

    def test_space_collection(self):
        url = "https://space.bilibili.com/3546708503956225/lists?sid=7428841"
        self.assertEqual(
            core.parse_collection_reference(url),
            ("3546708503956225", "7428841"),
        )
        self.assertIsNone(core.parse_collection_reference("BV1xx411c7mD"))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.dash = {
            "video": [
                item("https://cdn/1080", stream_id=80, bandwidth=9_000_000),
                item("https://cdn/4k", stream_id=120, bandwidth=18_000_000,
                     width=3840, height=2160),
            ],
            "dolby": {
                "video": [item("https://cdn/dv", stream_id=126,
                               bandwidth=22_000_000, width=3840, height=2160)],
                "audio": [
                    item("https://cdn/dolby-low", bandwidth=448_000, codecs="ec-3"),
                    item("https://cdn/dolby-best", bandwidth=1_024_000, codecs="ec-3"),
                ],
            },
            "flac": {
                "audio": item("https://cdn/hires", bandwidth=7_200_000, codecs="fLaC")
            },
        }

    def test_selects_highest_video_and_both_real_audio_tracks(self):
        video, hires, dolby, regular = core.select_media_streams(self.dash)
        self.assertEqual(video.urls[0], "https://cdn/dv")
        self.assertIn("Dolby Vision", video.description)
        self.assertEqual(hires.urls[0], "https://cdn/hires")
        self.assertEqual(dolby.urls[0], "https://cdn/dolby-best")
        self.assertIsNone(regular)

    def test_missing_hires_keeps_dolby_single_track(self):
        self.dash.pop("flac")
        video, hires, dolby, regular = core.select_media_streams(self.dash)
        self.assertIsNone(hires)
        self.assertEqual(dolby.urls[0], "https://cdn/dolby-best")
        self.assertIsNone(regular)

    def test_plain_video_uses_best_regular_audio(self):
        self.dash.pop("flac")
        self.dash["dolby"]["audio"] = []
        self.dash["dolby"]["video"] = []
        self.dash["audio"] = [
            item("https://cdn/aac-low", bandwidth=128_000, codecs="mp4a.40.2"),
            item("https://cdn/aac-best", bandwidth=320_000, codecs="mp4a.40.2"),
        ]
        video, hires, dolby, regular = core.select_media_streams(self.dash)
        self.assertEqual(video.urls[0], "https://cdn/4k")
        self.assertIsNone(hires)
        self.assertIsNone(dolby)
        self.assertEqual(regular.urls[0], "https://cdn/aac-best")

    def test_hires_only_does_not_require_dolby(self):
        self.dash["dolby"]["audio"] = []
        self.dash["dolby"]["video"] = []
        self.dash["audio"] = [item("https://cdn/aac", bandwidth=320_000, codecs="mp4a.40.2")]
        video, hires, dolby, regular = core.select_media_streams(self.dash)
        self.assertNotIn("Dolby Vision", video.description)
        self.assertEqual(hires.urls[0], "https://cdn/hires")
        self.assertIsNone(dolby)
        self.assertIsNone(regular)

    def test_dolby_vision_and_hires_work_without_atmos(self):
        self.dash["dolby"]["audio"] = []
        video, hires, dolby, regular = core.select_media_streams(self.dash)
        self.assertIn("Dolby Vision", video.description)
        self.assertEqual(hires.urls[0], "https://cdn/hires")
        self.assertIsNone(dolby)
        self.assertIsNone(regular)

    def test_dolby_vision_video_can_fall_back_to_regular_audio(self):
        self.dash.pop("flac")
        self.dash["dolby"]["audio"] = []
        self.dash["audio"] = [item("https://cdn/aac", bandwidth=320_000, codecs="mp4a.40.2")]
        video, hires, dolby, regular = core.select_media_streams(self.dash)
        self.assertIn("Dolby Vision", video.description)
        self.assertIsNone(hires)
        self.assertIsNone(dolby)
        self.assertEqual(regular.kind, "regular")

    def test_requires_at_least_one_audio_stream(self):
        self.dash.pop("flac")
        self.dash["dolby"]["audio"] = []
        self.dash["audio"] = []
        with self.assertRaisesRegex(RuntimeError, "音频"):
            core.select_media_streams(self.dash)

    def test_sanitizes_windows_filename(self):
        self.assertEqual(core.sanitize_filename('a:b/c*?"'), "a＿b＿c＿＿＿")


class Aria2Tests(unittest.TestCase):
    def test_uses_16_connections_resume_and_bilibili_headers(self):
        command = core._aria2_command(
            r"C:\\tools\\aria2c.exe",
            "https://cdn.example/video.m4s",
            Path(r"D:\\downloads\\video.m4s"),
            "SESSDATA=secret",
        )
        self.assertIn("--continue=true", command)
        self.assertIn("--split=16", command)
        self.assertIn("--max-connection-per-server=16", command)
        self.assertIn("--file-allocation=none", command)
        self.assertIn("--header=Cookie: SESSDATA=secret", command)
        self.assertEqual(command[-1], "https://cdn.example/video.m4s")

    def test_parses_live_percent_and_speed(self):
        line = "[#abc123 512MiB/928MiB(55%) CN:16 DL:23MiB ETA:18s]"
        self.assertEqual(core._parse_aria2_progress(line), (55, "23MiB"))
        self.assertIsNone(core._parse_aria2_progress("download started"))

    def test_wget2_uses_parallel_chunks_resume_and_bilibili_headers(self):
        command = core._wget2_command(
            r"C:\\tools\\wget2.exe",
            "https://cdn.example/video.m4s",
            Path(r"D:\\downloads\\video.m4s"),
            "SESSDATA=secret",
            8,
        )
        self.assertIn("--continue", command)
        self.assertIn("--chunk-size=1M", command)
        self.assertIn("--max-threads=8", command)
        self.assertIn("--header=Cookie: SESSDATA=secret", command)
        self.assertIn(f"--referer={core.bili.REFERER}", command)
        self.assertIn(r"--output-document=D:\downloads\video.m4s", command)
        self.assertEqual(command[-1], "https://cdn.example/video.m4s")

    def test_cdn_errors_downgrade_then_use_openssl(self):
        stream = core.Stream(
            "video", ("https://cdn-1/video", "https://cdn-2/video"),
            "4K Dolby Vision", "dvh1", 1,
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(core, "locate_tool", side_effect=lambda name: "aria2c" if name == "aria2c" else (_ for _ in ()).throw(RuntimeError("missing"))), \
                mock.patch.object(core, "_download_aria2", side_effect=RuntimeError("567")) as aria, \
                mock.patch.object(core, "_download_url_parallel", return_value=Path(td) / "video.m4s") as openssl:
            output = core.download_stream(stream, Path(td) / "video.m4s", "cookie")
        self.assertEqual(output.name, "video.m4s")
        self.assertEqual(aria.call_count, 6)
        self.assertEqual(
            [call.args[-1] for call in aria.call_args_list],
            [16, 16, 4, 4, 1, 1],
        )
        openssl.assert_called_once()

    def test_openssl_fallback_resumes_after_truncated_response(self):
        stream = core.Stream(
            "dolby", ("https://cdn-1/audio",),
            "Dolby Atmos", "ec-3", 1,
        )
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(core, "locate_tool", side_effect=RuntimeError("missing")), \
                mock.patch.object(core, "_download_url_parallel", side_effect=RuntimeError("range unavailable")), \
                mock.patch.object(
                    core, "_download_url",
                    side_effect=[RuntimeError("下载不完整：1 / 10 bytes"), Path(td) / "audio.m4s"],
                ) as openssl:
            output = core.download_stream(
                stream, Path(td) / "audio.m4s", "cookie",
            )
        self.assertEqual(output.name, "audio.m4s")
        self.assertEqual(openssl.call_count, 2)

    def test_split_ranges_are_contiguous_and_complete(self):
        ranges = core._split_ranges(10, 101, 8)
        self.assertEqual(ranges[0][0], 10)
        self.assertEqual(ranges[-1][1], 100)
        for left, right in zip(ranges, ranges[1:]):
            self.assertEqual(left[1] + 1, right[0])
        self.assertEqual(sum(end - start + 1 for start, end in ranges), 91)

    def test_existing_openssl_prefix_skips_aria2(self):
        stream = core.Stream(
            "video", ("https://cdn-1/video",), "4K", "hevc", 1,
        )
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "video.m4s"
            destination.write_bytes(b"valid-prefix")
            with mock.patch.object(core, "locate_tool", return_value="aria2c"), \
                    mock.patch.object(core, "_download_aria2") as aria, \
                    mock.patch.object(core, "_download_url_parallel", return_value=destination):
                output = core.download_stream(stream, destination, "cookie")
        self.assertEqual(output, destination)
        aria.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class MuxIntegrationTests(unittest.TestCase):
    def run_ffmpeg(self, *args):
        subprocess.run(
            [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
            check=True, capture_output=True,
        )

    def test_copy_mux_has_hires_track_1_and_default_dolby_track_2(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            video, hires, dolby, output = (
                td / "video.mkv", td / "hires.flac", td / "dolby.ec3", td / "out.mkv"
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=size=32x32:rate=1:duration=1",
                "-c:v", "ffv1", str(video),
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo", "-t", "1",
                "-c:a", "flac", str(hires),
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1", "-t", "1",
                "-c:a", "eac3", "-b:a", "640k", str(dolby),
            )
            media = core.Media(
                "BV1xx411c7mD", 123, "test",
                core.Stream("video", ("v",), "test video", "ffv1", 1),
                core.Stream("hires", ("h",), "Hi-Res", "flac", 1),
                core.Stream("dolby", ("d",), "Dolby", "eac3", 1),
                "",
            )
            core.mux_mkv(video, ((hires, media.hires), (dolby, media.dolby)), output, media)
            _, audio = core.probe_mkv(output, expected_audio_kinds=("hires", "dolby"))
            self.assertEqual([x["codec_name"] for x in audio], ["flac", "eac3"])
            self.assertEqual(audio[0]["tags"]["title"], "Hi-Res Lossless (FLAC)")
            self.assertEqual(audio[1]["tags"]["title"], "Dolby Atmos (E-AC-3 JOC)")

    def test_copy_mux_without_hires_keeps_dolby(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            video, dolby, output = td / "video.mkv", td / "dolby.ec3", td / "out.mkv"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=size=32x32:rate=1:duration=1",
                "-c:v", "ffv1", str(video),
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1", "-t", "1",
                "-c:a", "eac3", "-b:a", "640k", str(dolby),
            )
            media = core.Media(
                "BV1xx411c7mD", 123, "test",
                core.Stream("video", ("v",), "test video", "ffv1", 1),
                None,
                core.Stream("dolby", ("d",), "Dolby", "eac3", 1),
                "",
            )
            core.mux_mkv(video, ((dolby, media.dolby),), output, media)
            _, audio = core.probe_mkv(output, expected_audio_kinds=("dolby",))
            self.assertEqual([x["codec_name"] for x in audio], ["eac3"])
            self.assertEqual(audio[0]["tags"]["title"], "Dolby Atmos (E-AC-3 JOC)")

    def test_copy_mux_plain_aac_audio(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            video, regular, output = td / "video.mkv", td / "audio.m4a", td / "out.mkv"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=size=32x32:rate=1:duration=1",
                "-c:v", "ffv1", str(video),
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
                "-c:a", "aac", "-b:a", "192k", str(regular),
            )
            regular_stream = core.Stream("regular", ("a",), "Original AAC · 192 kb/s", "mp4a.40.2", 192_000)
            media = core.Media(
                "BV1xx411c7mD", 123, "plain",
                core.Stream("video", ("v",), "test video", "ffv1", 1),
                None, None, "", regular_stream,
            )
            core.mux_mkv(video, ((regular, regular_stream),), output, media)
            _, audio = core.probe_mkv(output, expected_audio_kinds=("regular",))
            self.assertEqual([x["codec_name"] for x in audio], ["aac"])
            self.assertEqual(audio[0]["tags"]["title"], "Bilibili Original Audio (mp4a.40.2)")

    def test_copy_mux_hires_only(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            video, hires, output = td / "video.mkv", td / "hires.flac", td / "out.mkv"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=size=32x32:rate=1:duration=1",
                "-c:v", "ffv1", str(video),
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo", "-t", "1",
                "-c:a", "flac", str(hires),
            )
            hires_stream = core.Stream("hires", ("h",), "Hi-Res", "flac", 1)
            media = core.Media(
                "BV1xx411c7mD", 123, "hires-only",
                core.Stream("video", ("v",), "test video", "ffv1", 1),
                hires_stream, None, "",
            )
            core.mux_mkv(video, ((hires, hires_stream),), output, media)
            _, audio = core.probe_mkv(output, expected_audio_kinds=("hires",))
            self.assertEqual([x["codec_name"] for x in audio], ["flac"])
            self.assertEqual(audio[0]["tags"]["title"], "Hi-Res Lossless (FLAC)")


class DolbyVisionMuxRoutingTests(unittest.TestCase):
    def test_dvh1_routes_to_mkvmerge_without_transcoding(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            video, dolby, output = td / "video.m4s", td / "audio.m4s", td / "out.mkv"
            video.touch()
            dolby.touch()
            media = core.Media(
                "BV1xx411c7mD", 123, "DV test",
                core.Stream("video", ("v",), "Dolby Vision", "hevc", 1),
                None,
                core.Stream("dolby", ("d",), "Dolby", "eac3", 1),
                "",
            )
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                Path(command[command.index("-o") + 1]).touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(core, "_probe_video_codec_tag", return_value="dvh1", create=True), \
                    mock.patch.object(core, "locate_tool", side_effect=lambda name: name), \
                    mock.patch.object(core.subprocess, "run", side_effect=fake_run):
                core.mux_mkv(video, ((dolby, media.dolby),), output, media)

            self.assertEqual(commands[0][0], "mkvmerge")
            self.assertIn("--no-audio", commands[0])
            self.assertNotIn("-c:v", commands[0])
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
