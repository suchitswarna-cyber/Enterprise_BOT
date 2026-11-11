"""Tamper-evident audit log.

Each entry is one JSON line holding the SHA-256 of the previous entry, so editing
or deleting any line breaks the chain and `verify()` reports where.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, event: str, **fields) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
            "prev": self._last,
        }
        entry["hash"] = _digest(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        self._last = entry["hash"]
        return entry

    def verify(self) -> tuple[bool, str]:
        if not self.path.exists():
            return True, "empty log"
        prev = GENESIS
        with self.path.open() as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("prev") != prev:
                    return False, f"chain broken at line {n} (prev hash mismatch)"
                if _digest(entry) != entry.get("hash"):
                    return False, f"entry modified at line {n} (hash mismatch)"
                prev = entry["hash"]
        return True, "chain intact"
