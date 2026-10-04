from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from potluck_local.services.contribution_policy import ContributionSchedule, admission_reason
from potluck_local.services.settings_manager import NetworkSettings, PotluckSettings, GatewaySettings, save_settings, get_settings


def test_overnight_schedule_uses_start_day_and_exclusive_end():
    n = NetworkSettings(contributor_mode=True, contributor_schedule={
        'start': '22:00', 'end': '06:00', 'days': [0], 'timezone': 'America/New_York'})
    for stamp, expected in [('2026-09-21T21:59:00', 'outside_schedule'), ('2026-09-21T22:00:00', None),
                            ('2026-09-22T05:59:00', None), ('2026-09-22T06:00:00', 'outside_schedule'),
                            ('2026-09-22T22:00:00', 'outside_schedule')]:
        assert admission_reason(n, now=datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo('America/New_York'))) == expected


def test_dst_fallback_is_evaluated_as_local_clock_time():
    n = NetworkSettings(contributor_mode=True, contributor_schedule={
        'start': '01:00', 'end': '02:00', 'timezone': 'America/New_York'})
    for fold in [0, 1]:
        now = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo('America/New_York'), fold=fold)
        assert admission_reason(n, now=now) is None


@pytest.mark.parametrize('schedule', [dict(start='25:00', end='06:00'), dict(start='10:00', end='10:00'),
    dict(start='10:00', end='11:00', timezone='not/a/zone'), dict(start='10:00', end='11:00', days=[0, 0])])
def test_bad_schedules_rejected_and_legacy_invalid_data_fails_closed(schedule):
    with pytest.raises(ValidationError): ContributionSchedule.model_validate(schedule)
    assert admission_reason(NetworkSettings(contributor_mode=True, contributor_schedule=schedule)) == 'invalid_schedule'


def test_disabled_job_cap_and_memory_admission():
    assert admission_reason(NetworkSettings()) == 'disabled'
    assert admission_reason(NetworkSettings(contributor_mode=True, max_concurrent_jobs=0)) == 'job_limit_zero'
    n = NetworkSettings(contributor_mode=True, contributor_min_free_memory_gib=2)
    assert admission_reason(n, available_gib=1.9) == 'low_memory'
    assert admission_reason(n, available_gib=2) is None


def test_controls_preserve_unrelated_settings_and_do_not_return_credentials(tmp_path, monkeypatch):
    from potluck_local.routers import contributor
    from potluck_local.services import contributor_service
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    save_settings(PotluckSettings(gateway=GatewaySettings(enabled=True, api_key='never-echo', pool_routing=True)))
    svc = SimpleNamespace(apply_settings=AsyncMock(), has_member_session=lambda: True,
        status=lambda: dict(state='stopped', accepting_jobs=False, jobs_in_flight=0, jobs_served=0, last_error=None))
    monkeypatch.setattr(contributor_service, 'get_contributor_service', lambda: svc)
    app = FastAPI(); app.include_router(contributor.router, prefix='/contributor')
    with TestClient(app) as client:
        response = client.patch('/contributor/controls', json={'maxJobs': 2, 'maxTokens': 512, 'minFreeMemoryGiB': 2})
        assert response.status_code == 200
        assert 'never-echo' not in response.text
        assert get_settings().gateway.pool_routing and get_settings().gateway.api_key == 'never-echo'
        assert not get_settings().network.contributor_mode
        assert svc.apply_settings.await_count == 1
        assert client.patch('/contributor/controls', json={'maxJobs': -1}).status_code == 422
        assert client.patch('/contributor/controls', json={'enabled': None}).status_code == 422
        assert get_settings().network.max_concurrent_jobs == 2
