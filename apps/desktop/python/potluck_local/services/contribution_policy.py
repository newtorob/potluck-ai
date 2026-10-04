"""Admission policy for new coordinator jobs, shared by app and CLI controls."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContributionSchedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    end: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    days: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    timezone: str | None = None

    @field_validator('days')
    @classmethod
    def valid_days(cls, days):
        if any(day not in range(7) for day in days) or len(set(days)) != len(days):
            raise ValueError('Use unique weekdays 0 (Monday) through 6 (Sunday).')
        return days

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        if value is not None:
            try: ZoneInfo(value)
            except Exception as exc: raise ValueError('Use an IANA timezone.') from exc
        return value

    @model_validator(mode='after')
    def distinct_times(self):
        if self.start == self.end:
            raise ValueError('Start and end must differ; remove the schedule for all-day service.')
        return self


def admission_reason(network, *, now: datetime | None = None, available_gib: float | None = None) -> str | None:
    if not network.contributor_mode: return 'disabled'
    if network.max_concurrent_jobs == 0: return 'job_limit_zero'
    if network.contributor_schedule is not None:
        try:
            schedule = ContributionSchedule.model_validate(network.contributor_schedule)
            now = (now or datetime.now().astimezone()).astimezone(ZoneInfo(schedule.timezone) if schedule.timezone else None)
            minute = now.strftime('%H:%M')
            overnight = schedule.start > schedule.end
            inside = (minute >= schedule.start or minute < schedule.end) if overnight else schedule.start <= minute < schedule.end
            day = (now - timedelta(days=1)).weekday() if overnight and minute < schedule.end else now.weekday()
            if not inside or day not in schedule.days: return 'outside_schedule'
        except Exception:
            return 'invalid_schedule'
    if network.contributor_min_free_memory_gib:
        try:
            available = psutil.virtual_memory().available / 1024 ** 3 if available_gib is None else available_gib
            if available < network.contributor_min_free_memory_gib: return 'low_memory'
        except Exception:
            return 'memory_unavailable'
    return None
