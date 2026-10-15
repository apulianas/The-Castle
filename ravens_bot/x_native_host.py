"""Edge native-messaging bridge: validate public posts and write the Docker feed."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

from .x_browser import SNAPSHOT_NAME, write_snapshot
from .x_snapshot import MAX_SNAPSHOT_BYTES, normalize_posts


LOGGER = logging.getLogger(__name__)
HOST_NAME = "com.thecastle.ravens_feed"
REGISTRY_KEY = rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}"
HOST_DIRECTORY = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "The-Castle" / "x-native-host"
EXTENSION_ID = re.compile(r"[a-p]{32}")


def read_exact(source: BinaryIO, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        part = source.read(size - len(parts))
        if not part:
            raise ValueError("Truncated native message")
        parts.extend(part)
    return bytes(parts)


def read_message(source: BinaryIO, *, allow_eof: bool = False) -> object:
    first = source.read(1)
    if not first:
        if allow_eof:
            raise EOFError
        raise ValueError("Truncated native message")
    header = first + read_exact(source, 3)
    size, = struct.unpack("=I", header)
    if not 0 < size <= MAX_SNAPSHOT_BYTES:
        raise ValueError("Invalid native message size")
    return json.loads(read_exact(source, size))


def send_message(destination: BinaryIO, payload: dict[str, object]) -> None:
    data = json.dumps(payload).encode("utf-8")
    destination.write(struct.pack("=I", len(data)))
    destination.write(data)
    destination.flush()


def update_feed(directory: Path, payload: object, now: datetime) -> dict[str, object]:
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported native message")
    if set(payload) == {"version", "error"} and payload["error"] == "collector_unavailable":
        write_snapshot(directory, {"version": 1, "error": "X extension stopped or needs attention"})
        return {"ok": True}
    if set(payload) != {"version", "posts"}:
        raise ValueError("Invalid native message fields")
    incoming = normalize_posts(payload["posts"], now)
    cutoff = (now - timedelta(days=7)).isoformat()
    if not any(post["created_at"] > cutoff for post in incoming):
        raise ValueError("No recent public Ravens posts")
    previous = []
    path = directory / SNAPSHOT_NAME
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Previous snapshot too large")
        saved = json.loads(raw)
        if not isinstance(saved, dict) or saved.get("version") != 1:
            raise ValueError("Invalid previous snapshot")
        if not saved.get("error"):
            previous = normalize_posts(saved.get("posts"), now)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        LOGGER.warning("Previous public snapshot could not be read; replacing it with newly collected posts.")
    combined = {post["url"]: post for post in [*previous, *incoming]}
    posts = sorted(
        (post for post in combined.values() if post["created_at"] > cutoff),
        key=lambda post: post["created_at"], reverse=True,
    )[:300]
    snapshot = {"version": 1, "fetched_at": now.isoformat(), "posts": posts}
    if len(json.dumps(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Public feed exceeds maximum snapshot size")
    write_snapshot(directory, snapshot)
    return {"ok": True, "count": len(posts), "fetched_at": now.isoformat()}


def serve(source: BinaryIO, destination: BinaryIO, directory: Path) -> None:
    """Serve one frame (also used by one-shot native messaging clients)."""
    try:
        result = update_feed(directory, read_message(source), datetime.now(timezone.utc))
    except (OSError, ValueError, RecursionError):
        result = failed_update(directory)
    send_message(destination, result)


def failed_update(directory: Path) -> dict[str, object]:
    LOGGER.warning("Rejected or could not save the public X feed.")
    try:
        write_snapshot(directory, {"version": 1, "error": "X extension feed unavailable"})
    except OSError:
        LOGGER.error("Could not invalidate the public feed; the bot will fall back when it expires.")
    return {"ok": False, "error": "Could not validate or save public Ravens posts. Check the feed folder."}


def serve_connection(source: BinaryIO, destination: BinaryIO, directory: Path) -> None:
    """Keep the writer alive until Edge closes its native messaging port."""
    while True:
        try:
            payload = read_message(source, allow_eof=True)
        except EOFError:
            return
        except (OSError, ValueError, RecursionError):
            send_message(destination, failed_update(directory))
            return  # A malformed frame cannot safely be resynchronized.
        try:
            result = update_feed(directory, payload, datetime.now(timezone.utc))
        except (OSError, ValueError, RecursionError):
            result = failed_update(directory)
        send_message(destination, result)


def batch_path(path: Path) -> str:
    text = str(path.resolve())
    if any(character in text for character in '"\r\n'):
        raise ValueError("Invalid launcher path")
    return '"' + text.replace("%", "%%") + '"'


def register(extension_id: str, directory: Path, host_directory: Path = HOST_DIRECTORY) -> Path:
    if not EXTENSION_ID.fullmatch(extension_id):
        raise ValueError("Edge extension ID must be 32 lowercase letters from a to p")
    if sys.platform != "win32":
        raise ValueError("Native host registration is supported on Windows only")
    import winreg

    source_package = Path(__file__).resolve().parent
    directory = directory.resolve()
    host_directory = host_directory.resolve()
    manifest_path = host_directory / f"{HOST_NAME}.json"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            current, _ = winreg.QueryValueEx(key, "")
        if Path(current).resolve() != manifest_path:
            raise ValueError("A different Ravens native host is already registered; remove it before replacing it")
    except FileNotFoundError:
        pass
    launcher = host_directory / "ravens-feed.cmd"
    command = (
        "@echo off\r\nsetlocal DisableDelayedExpansion\r\n"
        f"cd /d {batch_path(host_directory)} || exit /b 1\r\n"
        f"{batch_path(Path(sys.executable))} -m ravens_bot.x_native_host --output {batch_path(directory)}\r\n"
    )
    host_directory.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    installed_package = host_directory / "ravens_bot"
    installed_package.mkdir(exist_ok=True)
    for name in ("__init__.py", "x_native_host.py", "x_snapshot.py", "x_browser.py"):
        source = source_package / name
        destination = installed_package / name
        if source.resolve() != destination.resolve():
            destination.write_bytes(source.read_bytes())
    launcher.write_text(command, encoding="utf-8", newline="")
    manifest_path.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "The Castle public Ravens X feed writer",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }, indent=2), encoding="utf-8")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", metavar="EXTENSION_ID", help="Register the unpacked extension for this Windows user.")
    parser.add_argument("--output", type=Path, default=Path(".x-feed"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if args.register:
        try:
            manifest = register(args.register, args.output)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"{exc}\n")
        print(f"Registered Edge native host: {manifest}")
        print(f"Public feed folder: {args.output.resolve()}")
        return
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    serve_connection(sys.stdin.buffer, sys.stdout.buffer, args.output)


if __name__ == "__main__":
    main()
