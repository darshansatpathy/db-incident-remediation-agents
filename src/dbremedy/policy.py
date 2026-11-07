"""The operating policy: protections, hard caps and change freezes, enforced by the governor."""

from __future__ import annotations

import fnmatch
from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Protected(Strict):
    applications: list[str] = []
    users: list[str] = []

    def application(self, name: str) -> str | None:
        return next((p for p in self.applications if fnmatch.fnmatchcase(name, p)), None)


class NeverTouch(Strict):
    superusers: bool = True
    background_workers: bool = True


class PolicyLimits(Strict):
    max_terminations_per_incident: int = Field(default=60, ge=0)
    max_cancellations_per_incident: int = Field(default=20, ge=0)
    max_vacuums_per_incident: int = Field(default=3, ge=0)
    batch_approval_threshold: int = Field(default=10, ge=0)

    def cap(self, action: str) -> int:
        return {
            "terminate_backend": self.max_terminations_per_incident,
            "cancel_backend": self.max_cancellations_per_incident,
            "vacuum_analyze": self.max_vacuums_per_incident,
        }.get(action, 0)


class FreezeWindow(Strict):
    days: list[Day] = Field(min_length=1)
    start: str = "00:00"
    end: str = "23:59"
    timezone: str = "UTC"

    @field_validator("start", "end")
    @classmethod
    def hhmm(cls, value: str) -> str:
        time.fromisoformat(value)
        return value

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(ZoneInfo(self.timezone))
        if DAYS[local.weekday()] not in self.days:
            return False
        return time.fromisoformat(self.start) <= local.time().replace(tzinfo=None) <= time.fromisoformat(self.end)


class ChangeFreeze(Strict):
    active: bool = False
    windows: list[FreezeWindow] = []


class Policy(Strict):
    protected: Protected = Protected()
    never_touch: NeverTouch = NeverTouch()
    limits: PolicyLimits = PolicyLimits()
    change_freeze: ChangeFreeze = ChangeFreeze()
    dry_run: bool = False

    def frozen(self, moment: datetime, override: bool = False) -> bool:
        return override or self.change_freeze.active or any(w.contains(moment) for w in self.change_freeze.windows)


def load_policy(path: Path) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
