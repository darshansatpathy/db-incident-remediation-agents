"""Bundled runbooks, policy and scenarios: inside the wheel, or at the repository root in development."""

from __future__ import annotations

from pathlib import Path


def resource(name: str) -> Path:
    packaged = Path(__file__).resolve().parent / name
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / name
