from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3


class RawStore:
    """Append-only content-addressed storage for upstream responses."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, source: str, payload: bytes, suffix: str) -> tuple[str, Path]:
        digest = hashlib.sha256(payload).hexdigest()
        day = datetime.now(UTC).date().isoformat()
        destination = self.root / "raw" / source / day / f"{digest}.{suffix.lstrip('.')}"
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not destination.exists():
            atomic_write(destination, payload)
        return digest, destination

    def put_json(self, source: str, value: Any) -> tuple[str, Path]:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return self.put(source, payload, "json")


def atomic_write(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
