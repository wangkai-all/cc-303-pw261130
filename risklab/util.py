"""通用日期、JSON 与摘要工具。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object, length: int = 16) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()[:length]


def month_ends(start: date, end: date) -> list[date]:
    """返回区间内（含端点所在月）的月末日期。"""
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if m == 12:
            d = date(y, 12, 31)
            y, m = y + 1, 1
        else:
            d = date(y, m + 1, 1) - timedelta(days=1)
            m += 1
        if start <= d <= end:
            out.append(d)
    return out


def pct(part: int, total: int, digits: int = 1) -> float | None:
    if not total:
        return None
    return round(part * 100.0 / total, digits)
