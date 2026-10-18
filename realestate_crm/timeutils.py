from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_day_bounds(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def local_label(value: datetime | None, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%d %b %Y, %H:%M") if value else "—"
