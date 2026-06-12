"""
Staleness detection for weekly specials data.

Australian retail specials (Coles, Woolworths) reset Wednesday 00:00 AEST/AEDT.
Stored data is stale when it was synced before the most recent Wednesday
midnight (Sydney time). Freshness info is exposed only via GET /health —
the product data endpoints' response shape is frozen.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
SPECIALS_WEEKDAY = 2  # Wednesday (Monday=0)


def last_specials_reset(now: datetime | None = None) -> datetime:
    """The most recent Wednesday 00:00 Sydney time."""
    if now is None:
        now = datetime.now(SYDNEY_TZ)
    else:
        now = now.astimezone(SYDNEY_TZ)
    days_since = (now.weekday() - SPECIALS_WEEKDAY) % 7
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since)


def parse_synced_at(synced_at: str | None) -> datetime | None:
    """Parse an ISO timestamp; naive values are treated as UTC
    (older crawlers wrote naive UTC timestamps)."""
    if not synced_at:
        return None
    try:
        dt = datetime.fromisoformat(synced_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_stale(data: dict | None, now: datetime | None = None) -> bool:
    """True when stored data predates this week's specials reset (or is missing)."""
    if not data:
        return True
    synced_at = parse_synced_at(data.get("synced_at"))
    if synced_at is None:
        return True
    return synced_at < last_specials_reset(now)


def freshness_report(data: dict | None, now: datetime | None = None) -> dict:
    """Diagnostic freshness summary for /health."""
    if now is None:
        now = datetime.now(SYDNEY_TZ)
    if not data:
        return {
            "synced_at": None,
            "is_stale": True,
            "data_age_hours": None,
            "stale_reason": "No data stored",
        }

    synced_at = parse_synced_at(data.get("synced_at"))
    if synced_at is None:
        return {
            "synced_at": data.get("synced_at"),
            "is_stale": True,
            "data_age_hours": None,
            "stale_reason": "Missing or unparsable sync timestamp",
        }

    age_hours = round((now.astimezone(timezone.utc) - synced_at.astimezone(timezone.utc)).total_seconds() / 3600, 1)
    reset = last_specials_reset(now)
    stale = synced_at < reset

    report = {
        "synced_at": data.get("synced_at"),
        "is_stale": stale,
        "data_age_hours": age_hours,
        "stale_reason": None,
    }
    if data.get("crawl_status"):
        report["crawl_status"] = data["crawl_status"]
    if stale:
        report["stale_reason"] = (
            f"Data synced {age_hours}h ago, before this week's specials reset "
            f"on {reset.strftime('%A %d %b %H:%M %Z')}"
        )
    return report
