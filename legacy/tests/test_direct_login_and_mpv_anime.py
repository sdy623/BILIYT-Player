"""Direct-login and local MPV Anime regression tests (no real credentials required)."""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import json
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_backend():
    """Load the backend without installing optional runtime packages."""
    fake_requests = types.ModuleType("requests")
    fake_requests.Session = type("Session", (), {})
    with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
            mock.patch.object(sys, "frozen", True, create=True):
        spec = importlib.util.spec_from_file_location(
            "bili_clipboard_dolby_direct_test", ROOT / "bili_clipboard_dolby.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


D = load_backend()


class DirectLoginTests(unittest.TestCase):
    def test_reads_valid_firefox_sessdata_without_copying_it(self):
        with tempfile.TemporaryDirectory() as td:
            appdata = Path(td)
            profile = appdata / "Mozilla" / "Firefox" / "Profiles" / "default-release"
            profile.mkdir(parents=True)
            db = profile / "cookies.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER)")
            con.execute(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
                (".bilibili.com", "SESSDATA", "secret-in-memory", 4_102_444_800),
            )
            con.commit()
            con.close()

            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                value, source = D.import_browser_sessdata(browser="firefox")

            self.assertEqual(value, "secret-in-memory")
            self.assertEqual(source, "Firefox (default-release)")
            self.assertFalse((appdata / "BiliYTPlayer" / ".env").exists())

    def test_ignores_expired_firefox_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            appdata = Path(td)
            profile = appdata / "Mozilla" / "Firefox" / "Profiles" / "old"
            profile.mkdir(parents=True)
            con = sqlite3.connect(profile / "cookies.sqlite")
            con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER)")
            con.execute(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
                (".bilibili.com", "SESSDATA", "expired", 1),
            )
            con.commit()
            con.close()

            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                self.assertEqual(
                    D.import_browser_sessdata(browser="firefox"), (None, None)
                )

    def test_reads_only_bilibili_sessdata_from_edge(self):
        with tempfile.TemporaryDirectory() as td:
            localappdata = Path(td)
            user_data = localappdata / "Microsoft" / "Edge" / "User Data"
            profile = user_data / "Profile 2"
            network = profile / "Network"
            network.mkdir(parents=True)
            (user_data / "Local State").write_text("{}", encoding="utf-8")
            db = network / "Cookies"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            con.execute("INSERT INTO meta VALUES ('version', '23')")
            con.execute(
                "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
                "encrypted_value BLOB, expires_utc INTEGER)"
            )
            chrome_expiry = int((4_102_444_800 + 11_644_473_600) * 1_000_000)
            con.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
                (".bilibili.com", "SESSDATA", "edge-secret-in-memory", b"", chrome_expiry),
            )
            con.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
                (".example.com", "SESSDATA", "must-not-load", b"", chrome_expiry),
            )
            con.commit()
            con.close()

            messages = []
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(localappdata)}), \
                    mock.patch.object(D.sys, "platform", "win32"), \
                    mock.patch.object(D, "_edge_master_key", return_value=b"unused"):
                value, source = D.import_browser_sessdata(
                    emit=messages.append, browser="edge"
                )

            self.assertEqual(value, "edge-secret-in-memory")
            self.assertEqual(source, "Edge (Profile 2)")
            self.assertNotIn("edge-secret-in-memory", "\n".join(messages))
            self.assertNotIn("must-not-load", "\n".join(messages))

    def test_edge_app_bound_cookie_is_not_bypassed(self):
        with self.assertRaises(D.EdgeCookieUnsupported):
            D._decrypt_edge_cookie(b"v20" + b"x" * 64, ".bilibili.com", b"k" * 32, 24)

    def test_decrypts_edge_v10_cookie_and_checks_host_binding(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        host = ".bilibili.com"
        key = b"k" * 32
        nonce = b"n" * 12
        import hashlib
        plaintext = hashlib.sha256(host.encode()).digest() + b"edge-v10-secret"
        encrypted = b"v10" + nonce + AESGCM(key).encrypt(nonce, plaintext, None)

        self.assertEqual(
            D._decrypt_edge_cookie(encrypted, host, key, 24), "edge-v10-secret"
        )
        with self.assertRaises(ValueError):
            D._decrypt_edge_cookie(encrypted, ".example.com", key, 24)

    def test_rejects_unknown_browser_selector(self):
        with self.assertRaises(ValueError):
            D.import_browser_sessdata(browser="chrome")

    def test_edge_extension_bridge_is_loopback_token_gated_and_memory_only(self):
        D.stop_edge_session_bridge()
        messages = []
        token = D.start_edge_session_bridge(emit=messages.append, port=0)
        self.assertTrue(token)
        port = D._EDGE_BRIDGE_SERVER.server_port
        secret = "edge-extension-secret-value"
        payload = json.dumps({"token": token, "sessdata": secret}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/biliyt-session",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "chrome-extension://unit-test",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 204)
            with mock.patch.object(D, "_edge_sessdata_candidates", return_value=[]):
                self.assertEqual(
                    D.import_browser_sessdata(browser="edge"),
                    (secret, "Edge 扩展"),
                )
            self.assertNotIn(secret, "\n".join(messages))
        finally:
            D.stop_edge_session_bridge()

    def test_session_adds_public_device_cookies(self):
        nav = {
            "code": 0,
            "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/img-key.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/sub-key.png",
            }},
        }
        spi = {"code": 0, "data": {"b_3": "public-buvid3", "b_4": "public-buvid4"}}
        with mock.patch.object(D, "_fast_bili_api", side_effect=[nav, spi]):
            session, img, sub = D.init_bili_session("browser-sessdata")
        self.assertEqual(img, "img-key")
        self.assertEqual(sub, "sub-key")
        self.assertEqual(session._cookies["SESSDATA"], "browser-sessdata")
        self.assertEqual(session._cookies["buvid3"], "public-buvid3")
        self.assertEqual(session._cookies["buvid4"], "public-buvid4")
        self.assertIn("b_nut", session._cookies)


class MpvAnimeTests(unittest.TestCase):
    def test_uses_adjacent_mpv_anime_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            player = root / "mpv.exe"
            player.touch()
            config = root / "portable_config"
            config.mkdir()
            self.assertEqual(
                D._player_config_dir(str(player)).casefold(), str(config).casefold()
            )

    def test_environment_override_is_a_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            player = Path(td) / "mpv.exe"
            player.touch()
            with mock.patch.dict(os.environ, {"BILIYT_MPV_PATH": str(player)}):
                candidates = D._mpv_anime_candidates()
            self.assertIn(player.resolve(), candidates)


if __name__ == "__main__":
    unittest.main()
