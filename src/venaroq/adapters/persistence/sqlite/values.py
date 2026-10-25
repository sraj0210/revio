"""SQLite value conversions shared by cohesive persistence repositories."""

from datetime import UTC, datetime


def timestamp(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


def datetime_value(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
