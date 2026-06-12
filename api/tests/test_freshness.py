from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from services.freshness import last_specials_reset, is_stale, parse_synced_at, freshness_report

SYD = ZoneInfo("Australia/Sydney")


def test_last_reset_on_a_thursday():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=SYD)  # Thursday
    reset = last_specials_reset(now)
    assert reset == datetime(2026, 6, 10, 0, 0, tzinfo=SYD)  # Wednesday 00:00


def test_last_reset_on_wednesday_just_after_midnight():
    now = datetime(2026, 6, 10, 0, 5, tzinfo=SYD)  # Wednesday 00:05
    reset = last_specials_reset(now)
    assert reset == datetime(2026, 6, 10, 0, 0, tzinfo=SYD)  # same day


def test_last_reset_on_tuesday_night():
    now = datetime(2026, 6, 9, 23, 55, tzinfo=SYD)  # Tuesday 23:55
    reset = last_specials_reset(now)
    assert reset == datetime(2026, 6, 3, 0, 0, tzinfo=SYD)  # previous Wednesday


def test_data_synced_before_reset_is_stale():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=SYD)  # Thursday
    data = {"synced_at": "2026-06-09T00:00:00+00:00"}  # Tuesday UTC
    assert is_stale(data, now) is True


def test_data_synced_after_reset_is_fresh():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=SYD)  # Thursday
    data = {"synced_at": "2026-06-10T01:00:00+10:00"}  # Wednesday 01:00 Sydney
    assert is_stale(data, now) is False


def test_naive_timestamp_treated_as_utc():
    # Woolies crawler historically wrote naive UTC timestamps
    dt = parse_synced_at("2026-05-24T04:10:56.415093")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_missing_data_is_stale():
    assert is_stale(None) is True
    assert is_stale({}) is True
    assert is_stale({"synced_at": "not-a-date"}) is True


def test_wednesday_morning_with_last_week_data_is_stale():
    # The core scenario: frontend fetches Wednesday 09:00, data from last Thursday
    now = datetime(2026, 6, 10, 9, 0, tzinfo=SYD)  # Wednesday 09:00
    data = {"synced_at": "2026-06-04T10:00:00+10:00"}  # last Thursday
    assert is_stale(data, now) is True


def test_freshness_report_fresh():
    now = datetime(2026, 6, 11, 10, 0, tzinfo=SYD)
    data = {"synced_at": "2026-06-10T01:00:00+10:00", "crawl_status": "success"}
    report = freshness_report(data, now)
    assert report["is_stale"] is False
    assert report["stale_reason"] is None
    assert report["crawl_status"] == "success"
    assert report["data_age_hours"] == 33.0


def test_freshness_report_no_data():
    report = freshness_report(None)
    assert report["is_stale"] is True
    assert report["data_age_hours"] is None
