from __future__ import annotations

import io
import json
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ravens_bot.x_feed import read_snapshot
from ravens_bot.x_native_host import (
    HOST_NAME, REGISTRY_KEY, batch_path, read_message, register, serve, update_feed,
)
from ravens_bot.x_snapshot import MAX_SNAPSHOT_BYTES


NOW = datetime(2026, 10, 14, 1, tzinfo=timezone.utc)


def message(post_id="123", age=1):
    return {"version": 1, "posts": [{
        "url": f"https://x.com/Ravens/status/{post_id}",
        "text": "WR Chris Moore (knee) has been ruled out.",
        "created_at": (NOW - timedelta(hours=age)).isoformat(),
        "image_urls": [],
    }]}


def frame(payload):
    data = json.dumps(payload).encode()
    return struct.pack("=I", len(data)) + data


def test_native_posts_match_bot_snapshot_contract(tmp_path):
    reply = update_feed(tmp_path, message(), NOW)
    assert reply == {"ok": True, "count": 1, "fetched_at": NOW.isoformat()}
    post, = read_snapshot(tmp_path / "ravens.json", NOW)
    assert post.url == "https://x.com/Ravens/status/123"
    assert post.source == "X"


def test_retains_recent_posts_and_updates_changed_post(tmp_path):
    update_feed(tmp_path, message(), NOW)
    update_feed(tmp_path, message("456", age=2), NOW)
    changed = message()
    changed["posts"][0]["text"] = "WR Chris Moore (ankle) has been ruled out."
    reply = update_feed(tmp_path, changed, NOW)
    assert reply["count"] == 2
    posts = read_snapshot(tmp_path / "ravens.json", NOW)
    assert "ankle" in posts[0].text
    assert posts[1].url.endswith("/456")


def test_native_merge_is_bounded_and_prunes_old_posts(tmp_path):
    payload = message()
    payload["posts"] = [message(str(index))["posts"][0] for index in range(300)]
    update_feed(tmp_path, payload, NOW)
    assert update_feed(tmp_path, message("301", age=0), NOW)["count"] == 300
    assert update_feed(tmp_path, {
        "version": 1, "posts": [dict(message()["posts"][0], created_at=(NOW + timedelta(days=8)).isoformat())],
    }, NOW + timedelta(days=8))["count"] == 1


@pytest.mark.parametrize("payload", [
    {"version": True, "posts": []},
    {"version": 1, "posts": [], "cookies": "must not be saved"},
    {"version": 1, "error": "untrusted error wording"},
    {"version": 1, "posts": [dict(message()["posts"][0], cookies="must not be saved")]},
    {"version": 1, "posts": [dict(message()["posts"][0], url="https://x.com/messages")]},
    {"version": 1, "posts": [dict(message()["posts"][0], text="x" * 25001)]},
    {"version": 1, "posts": [dict(message()["posts"][0], image_urls=["https://pbs.twimg.com/media/a"] * 5)]},
    message(age=24 * 8),
])
def test_rejects_non_public_or_invalid_inputs(tmp_path, payload):
    with pytest.raises(ValueError):
        update_feed(tmp_path, payload, NOW)
    assert not (tmp_path / "ravens.json").exists()


def test_stop_invalidates_feed_immediately(tmp_path):
    update_feed(tmp_path, message(), NOW)
    assert update_feed(tmp_path, {"version": 1, "error": "collector_unavailable"}, NOW) == {"ok": True}
    with pytest.raises(ValueError, match="unavailable"):
        read_snapshot(tmp_path / "ravens.json", NOW)


@pytest.mark.parametrize("data", [
    b"", b"\x01", struct.pack("=I", 0),
    struct.pack("=I", MAX_SNAPSHOT_BYTES + 1),
    struct.pack("=I", 20) + b"{}",
    struct.pack("=I", 1) + b"\xff",
])
def test_malformed_frame_returns_error_and_invalidates(tmp_path, data):
    update_feed(tmp_path, message(), NOW)
    output = io.BytesIO()
    serve(io.BytesIO(data), output, tmp_path)
    reply = read_message(io.BytesIO(output.getvalue()))
    assert reply["ok"] is False
    with pytest.raises(ValueError, match="unavailable"):
        read_snapshot(tmp_path / "ravens.json", NOW)


def test_partial_pipe_reads():
    class ShortReader(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 2))

    assert read_message(ShortReader(frame(message()))) == message()


def test_native_process_stdout_is_only_framed_json(tmp_path):
    payload = message()
    payload["posts"][0]["created_at"] = datetime.now(timezone.utc).isoformat()
    process = subprocess.run(
        [sys.executable, "-m", "ravens_bot.x_native_host", "--output", str(tmp_path)],
        input=frame(payload), capture_output=True, check=True,
    )
    output = io.BytesIO(process.stdout)
    assert read_message(output)["ok"] is True
    assert output.read() == b""
    assert (tmp_path / "ravens.json").exists()


def test_register_writes_scoped_manifest_and_quotes_paths(tmp_path):
    registry = MagicMock()
    registry.OpenKey.side_effect = FileNotFoundError
    extension_id = "a" * 32
    host_directory = tmp_path / "host files"
    with patch.dict(sys.modules, {"winreg": registry}), patch("sys.platform", "win32"):
        path = register(extension_id, tmp_path / "public feed", host_directory)
    manifest = json.loads(path.read_text())
    assert manifest["name"] == HOST_NAME
    assert manifest["allowed_origins"] == [f"chrome-extension://{extension_id}/"]
    assert manifest["type"] == "stdio"
    launcher = Path(manifest["path"]).read_text()
    assert f'"{sys.executable}"' in launcher
    assert f'--output "{tmp_path / "public feed"}"' in launcher
    assert f'cd /d "{host_directory}"' in launcher
    assert {
        path.name for path in (host_directory / "ravens_bot").glob("*.py")
    } == {"__init__.py", "x_native_host.py", "x_snapshot.py", "x_browser.py"}
    registry.CreateKey.assert_called_once_with(registry.HKEY_CURRENT_USER, REGISTRY_KEY)
    if sys.platform == "win32":
        payload = message()
        payload["posts"][0]["created_at"] = datetime.now(timezone.utc).isoformat()
        process = subprocess.run(
            [manifest["path"]], input=frame(payload), capture_output=True, check=True,
        )
        output = io.BytesIO(process.stdout)
        assert read_message(output)["ok"] is True
        assert output.read() == b""
        assert (tmp_path / "public feed" / "ravens.json").exists()


def test_registration_refuses_unrelated_existing_host(tmp_path):
    registry = MagicMock()
    registry.QueryValueEx.return_value = (str(tmp_path / "another.json"), 1)
    with patch.dict(sys.modules, {"winreg": registry}), patch("sys.platform", "win32"):
        with pytest.raises(ValueError, match="different"):
            register("a" * 32, tmp_path / "feed", tmp_path / "host")
    registry.CreateKey.assert_not_called()
    assert not (tmp_path / "host").exists()


def test_registration_rejects_untrusted_extension_id(tmp_path):
    with pytest.raises(ValueError, match="extension ID"):
        register("bad/origin", tmp_path)


def test_launcher_escapes_environment_expansion(tmp_path):
    assert "%%PATH%%" in batch_path(tmp_path / "%PATH%")
