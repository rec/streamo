from unittest import mock

import pytest
from pytest_regressions.data_regression import DataRegressionFixture
from reccy.protocol import ipc, rpc

from streamo import runtime
from streamo.control import ControlController
from streamo.providers import ServiceCapability
from streamo.runtime import HealthWarnings, RuntimeState


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [100.0]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: value[0])
    monkeypatch.setattr(runtime.time, 'time', lambda: 1000 + value[0])
    return value


def test_incidents_retain_loss_and_recovery_without_duplicate_polls(
    clock: list[float], data_regression: DataRegressionFixture
) -> None:
    state = RuntimeState()
    controller = ControlController(state)
    state.set_audio_health('Audio unavailable', 48000)
    clock[0] += 1
    state.set_audio_health('Audio unavailable', 96000)
    clock[0] += 1
    state.set_audio_health(None, 96000)
    response = controller.handle_request(rpc.Request(command='incidents'))
    data_regression.check(response)
    assert controller.handle_request(rpc.Request(command='incidents')) == response
    assert (
        controller.handle_request(rpc.Request(command='status'))['incident_sequence']
        == 4
    )
    assert state.events_since(4)['events'] == []


def test_expired_history_is_explicit(clock: list[float]) -> None:
    state = RuntimeState()
    for _ in range(51):
        state.set_remote_health(None, 'Provider unavailable')
        state.set_remote_health(None)
    response = state.events_since(0)
    assert response['history_lost'] is True
    assert response['oldest_sequence'] == 3
    assert response['latest_sequence'] == 102
    assert len(response['events']) == 100
    assert state.events_since(102)['history_lost'] is False
    assert state.events_since(103)['history_lost'] is True


@pytest.mark.parametrize('after', [-1, True, '0', 1.5])
def test_incident_cursor_requires_nonnegative_integer(after: object) -> None:
    response = ControlController(RuntimeState()).handle_request(
        rpc.Request(command='incidents', params={'after': after})
    )
    assert isinstance(response, ipc.Error)


def test_silence_requires_sustained_recent_audio_and_is_suppressed_by_mute(
    clock: list[float],
) -> None:
    state = RuntimeState(HealthWarnings(silence_seconds=2))
    for second in range(3):
        clock[0] = 100 + second
        state.record_audio(
            frames=48000,
            sample_rate=48000,
            left_level_db=-100,
            right_level_db=-100,
            clipping=False,
        )
        state.evaluate_warnings()
    assert state.snapshot()['warnings'] == ['silence']
    sequence = state.snapshot()['incident_sequence']
    state.evaluate_warnings()
    assert state.snapshot()['incident_sequence'] == sequence
    state.set_muted(True)
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == []
    state.set_muted(False)
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == []
    clock[0] += 20
    state.record_audio(
        frames=48000,
        sample_rate=48000,
        left_level_db=-100,
        right_level_db=-100,
        clipping=False,
    )
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == []


def test_clipping_clears_when_signal_recovers(clock: list[float]) -> None:
    state = RuntimeState(HealthWarnings(clipping_seconds=1))
    for second in range(2):
        clock[0] = 100 + second
        state.record_audio(
            frames=48000,
            sample_rate=48000,
            left_level_db=0,
            right_level_db=-10,
            clipping=True,
        )
        state.evaluate_warnings()
    assert state.snapshot()['warnings'] == ['clipping']
    state.record_audio(
        frames=48000,
        sample_rate=48000,
        left_level_db=-10,
        right_level_db=-10,
        clipping=False,
    )
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == []
    assert state.snapshot()['incident_sequence'] == 2


def test_output_stall_does_not_clear_until_media_time_advances(
    clock: list[float],
) -> None:
    state = RuntimeState()
    state.begin_encoder_attempt()
    state.set_ffmpeg(alive=True)
    state.record_output_progress(1000)
    clock[0] += 10
    state.record_output_progress(1000)
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == ['output_stalled']
    state.record_output_progress(2000)
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == []
    assert state.snapshot()['incident_sequence'] == 2


def test_retry_error_is_retained_until_output_resumes(clock: list[float]) -> None:
    state = RuntimeState()
    state.publish_failed('FFmpeg exited with 1', 1)
    state.begin_encoder_attempt()
    state.publish_failed('FFmpeg exited with 1', 2)
    assert state.snapshot()['incident_sequence'] == 1
    state.begin_encoder_attempt()
    state.set_ffmpeg(alive=True)
    assert state.snapshot()['publish_error'] is not None
    state.record_output_progress(1000)
    assert state.snapshot()['publish_error'] is None
    assert state.snapshot()['incident_sequence'] == 2


def test_provider_staleness_is_separate_from_successful_query(
    clock: list[float],
) -> None:
    state = RuntimeState()
    service = mock.Mock()
    service.service.service = 'youtube'
    service.service.ingest = None

    service.capabilities = [ServiceCapability.HEALTH]
    state.configure_service(service)
    clock[0] += 90
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == ['provider_health_stale']
    state.set_remote_health(None, 'Provider unavailable')
    state.evaluate_warnings()
    assert state.snapshot()['warnings'] == []
    assert state.snapshot()['remote_health_error'] == 'Provider unavailable'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_warning_durations_must_be_finite_and_nonnegative(value: float) -> None:
    with pytest.raises(ValueError):
        HealthWarnings(silence_seconds=value)
