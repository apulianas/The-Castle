from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
CHANNEL_KEY_SEPARATOR = "@"
_LEGACY_DATED_TRANSACTION_KEY = re.compile(r"^transaction:\d{4}-\d{2}-\d{2}:")


def channel_key(key: str, target: int | str) -> str:
    return f"{key}{CHANNEL_KEY_SEPARATOR}{target}"


def migrate_key(key: str) -> str:
    """Drop the date from keys written before transactions were keyed by id alone."""
    return _LEGACY_DATED_TRANSACTION_KEY.sub("transaction:", key, count=1)


def _load_keys(raw: str) -> set[str]:
    """Both spellings of a stored key, since one migration now runs backwards.

    ESPN's NFL transactions carry no id, so the fallback identity is
    "date:description" and a current key looks exactly like the legacy dated
    form the migration strips. Rewriting it on load would leave nothing matching
    the key the bot computes, and every move would be announced again after a
    restart. Keeping both means old keys still suppress and current ones match.
    """
    return {raw, migrate_key(raw)}


class AnnouncementState:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._announced: set[str] = set()

    def load(self) -> None:
        try:
            if not self._path.exists():
                self._announced = set()
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("announced", []) if isinstance(data, dict) else []
            self._announced = {
                key for item in raw for key in _load_keys(str(item))
            }
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not read announcement state; starting fresh: %s", exc)
            self._announced = set()

    def unseen(self, key: str) -> bool:
        return key not in self._announced

    def mark(self, key: str) -> None:
        self._announced.add(key)
        self.save()

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {"announced": sorted(self._announced)}
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("Could not write announcement state: %s", exc)
