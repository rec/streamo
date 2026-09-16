import errno
import io
import tempfile
from unittest import mock

import pytest
from reccy.protocol import rpc

from streamo import streamer
from streamo.config import Streamo
from streamo.control import ControlController, RuntimeState
from streamo.provider_config import CustomService
from streamo.providers import GenericServiceAdapter


class Clock:
    now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


class Encoder:
    def __init__(self, controller: ControlController, code: int | None) -> None:
        self.controller = controller
        self.returncode = code
        self.stdin = tempfile.TemporaryFile()
        self.stderr = io.BytesIO(b'failed rtmps://example.test/live/secret\n')

    def poll(self) -> int | None:
        if self.returncode is None:
            self.controller.handle_request(rpc.Request(command='stop'))
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15


@pytest.fixture
def config() -> Streamo:
    return Streamo(
        device_name='unused',
        channel=1,
        recover_publish=True,
        streaming_service=CustomService.model_validate(
            {
                'service': 'custom',
                'ingest': {
                    'protocol': 'rtmps',
                    'server_url': 'rtmps://example.test/live',
                    'stream_key': 'secret',
                },
                'encoding': {
                    'container': 'flv',
                    'audio': {
                        'codec': 'aac',
                        'bitrate': '160k',
                        'sample_rate': 48000,
                        'channels': 2,
                    },
                },
            }
        ),
    )


def test_retries_preserve_session_and_clean_every_encoder(
    config: Streamo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    controller = ControlController(RuntimeState())
    controller.state.set_muted(True)
    service = GenericServiceAdapter(config.streaming_service)
    clock = Clock()
    monkeypatch.setattr(streamer.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(streamer.time, 'sleep', clock.sleep)
    encoders: list[Encoder] = []
    starts: list[float] = []
    audio = mock.Mock()

    def launch(*args: object, **kwargs: object) -> Encoder:
        starts.append(clock.now)
        encoder = Encoder(controller, [1, 0, None][len(encoders)])
        encoders.append(encoder)
        return encoder

    with (
        mock.patch.object(streamer.subprocess, 'Popen', side_effect=launch),
        mock.patch.object(streamer, 'AudioCapture', return_value=audio) as capture,
        mock.patch.object(streamer, 'HealthPoller'),
        mock.patch.object(service, 'prepare', wraps=service.prepare) as prepare,
        mock.patch.object(service, 'publish', wraps=service.publish) as publish,
        mock.patch.object(service, 'finish', wraps=service.finish) as finish,
    ):
        assert (
            streamer.stream(config, controller, service, initial_image_paths=set()) == 0
        )
    assert starts == pytest.approx([100, 101, 103])
    assert controller.state.snapshot()['encoder_attempts'] == 3
    assert controller.state.snapshot()['muted'] is True
    assert controller.state.snapshot()['publish_requested'] is False
    assert controller.state.snapshot()['next_retry_at'] is None
    assert all(e.stdin.closed and e.stderr.closed for e in encoders)
    assert 'secret' not in caplog.text
    prepare.assert_called_once()
    publish.assert_called_once()
    finish.assert_called_once()
    capture.assert_called_once()
    audio.close_capture.assert_called_once()


def test_stop_during_backoff_prevents_another_attempt(
    config: Streamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = ControlController(RuntimeState())
    clock = Clock()
    monkeypatch.setattr(streamer.time, 'monotonic', clock.monotonic)

    def sleep(duration: float) -> None:
        clock.sleep(duration)
        controller.handle_request(rpc.Request(command='stop'))

    monkeypatch.setattr(streamer.time, 'sleep', sleep)
    encoder = Encoder(controller, 1)
    with (
        mock.patch.object(streamer.subprocess, 'Popen', return_value=encoder) as launch,
        mock.patch.object(streamer, 'AudioCapture') as capture,
        mock.patch.object(streamer, 'HealthPoller'),
    ):
        assert (
            streamer.stream(
                config,
                controller,
                GenericServiceAdapter(config.streaming_service),
                initial_image_paths=set(),
            )
            == 0
        )
    launch.assert_called_once()
    capture.return_value.update.assert_called_once()
    assert clock.now < 101
    assert controller.state.snapshot()['state'] == 'stopped'


@pytest.mark.parametrize(
    ('error_number', 'expected_attempts'), [(errno.EAGAIN, 2), (errno.ENOENT, 1)]
)
def test_only_temporary_launch_errors_retry(
    config: Streamo,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    expected_attempts: int,
) -> None:
    controller = ControlController(RuntimeState())
    clock = Clock()
    monkeypatch.setattr(streamer.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(streamer.time, 'sleep', clock.sleep)
    encoder = Encoder(controller, None)
    with (
        mock.patch.object(
            streamer.subprocess,
            'Popen',
            side_effect=[OSError(error_number, 'private details'), encoder],
        ) as launch,
        mock.patch.object(streamer, 'AudioCapture'),
        mock.patch.object(streamer, 'HealthPoller'),
    ):
        result = streamer.stream(
            config,
            controller,
            GenericServiceAdapter(config.streaming_service),
            initial_image_paths=set(),
        )
    assert launch.call_count == expected_attempts
    assert result == (1 if error_number == errno.ENOENT else 0)
    assert 'private details' not in str(controller.state.snapshot())
    encoder.stdin.close()
    encoder.stderr.close()


def test_recovery_disabled_preserves_failure_exit(config: Streamo) -> None:
    controller = ControlController(RuntimeState())
    encoder = Encoder(controller, 7)
    with (
        mock.patch.object(streamer.subprocess, 'Popen', return_value=encoder) as launch,
        mock.patch.object(streamer, 'AudioCapture'),
        mock.patch.object(streamer, 'HealthPoller'),
    ):
        assert (
            streamer.stream(
                config.model_copy(update={'recover_publish': False}),
                controller,
                GenericServiceAdapter(config.streaming_service),
                initial_image_paths=set(),
            )
            == 7
        )
    launch.assert_called_once()
    assert controller.state.snapshot()['state'] == 'failed'


def test_output_must_advance_for_sixty_seconds_before_backoff_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(streamer.time, 'monotonic', clock.monotonic)
    state = RuntimeState()
    state.begin_encoder_attempt()
    for second in range(61):
        clock.now = 100 + second
        state.record_output_progress((second + 1) * 1000000)
    assert state.output_is_stable()
    clock.now += 11
    assert not state.output_is_stable()
    state.record_output_progress(62000000)
    assert not state.output_is_stable()
    state.begin_encoder_attempt()
    assert state.snapshot()['last_output_progress_at'] is None
