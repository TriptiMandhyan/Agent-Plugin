#!/usr/bin/env python3
"""Shared utility functions for DRL pipeline scripts."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON file with proper formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def auth_header_from_env() -> str:
    """Build Basic auth header from ADO_PAT environment variable."""
    pat = os.environ.get("ADO_PAT", "").strip()
    if not pat:
        raise RuntimeError("ADO_PAT environment variable is required.")
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return f"Basic {token}"
