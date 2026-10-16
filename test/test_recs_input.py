import queue
import threading
import wave
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from pydantic import ValidationError
from recs.base.audio_stream import AudioHeader, AudioSubscription
from recs.daemon import instances

from streamo import preflight, recs_input, streamer
from streamo.closing import ClosingSequence
from streamo.config import Streamo
from streamo.recs_input import (
    AudioFormatChanged,
    RecsAudio,
    RecsConnection,
    RecsInput,
    RecsInputLost,
    RecsReceiver,
)
from streamo.runtime import RuntimeState


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, 48_000, 0, 'NONE', 'not compressed'))
        output.writeframes((samples * 32767).astype('<i2').tobytes())


@pytest.fixture
def samples(tmp_path: Path) -> np.ndarray:
    result = np.full((48_000, 2), 0.25, dtype=np.float32)
    result[:, 1] = -0.5
    write_wav(tmp_path / 'input.wav', result)
    return result


@pytest.fixture
def settings() -> RecsInput:
    return RecsInput(daemon=True, source='X18', channels=[17, 18])


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(recs_input.time, 'monotonic', lambda: now[0])
    return now


@pytest.fixture
def config(settings: RecsInput, tmp_path: Path) -> Streamo:
    return Streamo(
        recs_input=settings,
        live_overlays=False,
        image_dir=[tmp_path],
        streaming_service={
            'service': 'custom',
            'ingest': {
                'protocol': 'rtmps',
                'server_url': 'rtmps://example.test/live',
                'stream_key': 'not-a-real-key',
            },
            'encoding': {
                'container': 'flv',
                'audio': {
                    'codec': 'aac',
                    'bitrate': '160k',
                    'sample_rate': 48_000,
                    'channels': 2,
                },
            },
        },
    )


class FakeRpc:
    def __init__(self) -> None:
        self.identity = instances.InstanceIdentity(
            pid=1234, start_token='instance', started_at=1, role='daemon'
        )
        self.commands: list[str] = []
        self.state = 'idle'
        self.paused = False
        self.muted = False
        self.enabled = True
        self.sample_rate = 48_000
        self.generation = 0

    def call(self, command: str, **params: object) -> dict[str, object]:
        self.commands.append(command)
        if command == 'capabilities':
            return {
                'type': 'capabilities_result',
                'commands': ['subscribe_audio', 'unsubscribe_audio', 'audio_status'],
                'version': 11,
                'instance': self.identity.model_dump(),
            }
        if command == 'audio_status':
            return {
                'type': 'audio_status_result',
                'enabled': self.enabled,
                'paused': self.paused,
                'sources': [
                    {'source': 'X18', 'state': self.state, 'muted': self.muted}
                ],
            }
        if command == 'list_devices':
            return {
                'type': 'devices',
                'devices': [
                    {
                        'name': 'X18',
                        'channels': 18,
                        'sample_rate': self.sample_rate,
                        'online': True,
                    }
                ],
            }
        if command == 'subscribe_audio':
            self.generation += 1
            offer = AudioSubscription(
                subscription_id=f'offer-{self.generation}',
                source='X18',
                clock_id='clock',
                instance_token=self.identity.start_token,
                port=12345,
                sample_rate=self.sample_rate,
                channels=[17, 18],
                generation=self.generation,
                expires_at=recs_input.time.monotonic() + 5,
                secret='private-capability-must-not-appear',
            )
            return {
                'type': 'audio_subscribed',
                'instance': self.identity.model_dump(),
                'subscription': offer.model_dump(),
            }
        if command == 'unsubscribe_audio':
            return {'type': 'audio_unsubscribed', 'stopped': True}
        raise AssertionError(command)


@pytest.fixture
def rpc_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRpc:
    endpoint = tmp_path / 'control.sock'
    endpoint.touch(mode=0o600)
    server = FakeRpc()
    monkeypatch.setattr(
        recs_input.instances,
        'resolve',
        lambda selection: instances.Target(
            control_endpoint=str(endpoint), event_endpoint='unused'
        ),
    )
    monkeypatch.setattr(recs_input.rpc, 'Client', lambda *args, **kwargs: server)
    return server


@pytest.mark.parametrize(
    'field,value', [('device_name', 'X18'), ('channel', 17), ('sample_rate', 48_000)]
)
def test_recs_input_cannot_be_combined_with_device_capture(
    config: Streamo, field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match='cannot be combined'):
        Streamo(
            recs_input=config.recs_input,
            live_overlays=False,
            streaming_service=config.streaming_service,
            **{field: value},
        )


@pytest.mark.parametrize(
    'selection',
    [
        {'daemon': False},
        {'daemon': True, 'instance': 1234},
        {'instance': -1},
        {'daemon': True, 'channels': [17, 17]},
        {'daemon': True, 'channels': [17]},
    ],
)
def test_recs_input_requires_explicit_instance_and_distinct_stereo_channels(
    selection: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RecsInput.model_validate({'source': 'X18', 'channels': [17, 18]} | selection)


@pytest.mark.parametrize('probe', [False, True])
def test_recs_preflight_never_subscribes_or_probes_hardware(
    config: Streamo,
    rpc_server: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
    probe: bool,
) -> None:
    for name in ('query_devices', 'check_input_settings', 'InputStream'):
        monkeypatch.setattr(
            preflight.sounddevice,
            name,
            mock.Mock(side_effect=AssertionError('hardware probed')),
        )
    checks = preflight.check_audio(config, probe_device=probe)
    assert checks[0].status == 'pass'
    assert checks[-1].status == 'skipped'
    assert rpc_server.commands == ['capabilities', 'audio_status', 'list_devices']


@pytest.mark.parametrize(
    'condition,expected',
    [
        ('paused', 'warning'),
        ('muted', 'warning'),
        ('disabled', 'fail'),
        ('busy', 'warning'),
    ],
)
def test_preflight_reports_intentional_pause_and_forwarding_availability(
    config: Streamo, rpc_server: FakeRpc, condition: str, expected: str
) -> None:
    if condition == 'paused':
        rpc_server.paused = True
    elif condition == 'muted':
        rpc_server.muted = True
    elif condition == 'disabled':
        rpc_server.enabled = False
    else:
        rpc_server.state = 'connected'
    assert preflight.check_audio(config, probe_device=False)[0].status == expected
    assert 'subscribe_audio' not in rpc_server.commands


def test_connection_verifies_instance_selection_and_format(
    settings: RecsInput, rpc_server: FakeRpc
) -> None:
    connection = RecsConnection(settings)
    info = connection.inspect()
    response = connection.subscribe(info)
    assert response.instance == info.instance
    assert response.subscription.channels == [17, 18]
    assert response.subscription.sample_rate == 48_000
    connection.cancel(response.subscription.subscription_id)
    assert rpc_server.commands[-1] == 'unsubscribe_audio'


def test_explicit_pid_is_pinned_and_never_reselected(
    settings: RecsInput, rpc_server: FakeRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = settings.model_copy(update={'daemon': False, 'instance': 1234})
    target = instances.Target(
        control_endpoint='selected',
        event_endpoint='unused',
        identity=rpc_server.identity,
    )
    resolve = mock.Mock(return_value=target)
    monkeypatch.setattr(recs_input.instances, 'resolve', resolve)
    connection = RecsConnection(selected)
    connection.inspect()
    rpc_server.identity = rpc_server.identity.model_copy(
        update={'start_token': 'different-instance'}
    )
    with pytest.raises(ValueError, match='identity changed'):
        connection.inspect()
    assert resolve.call_count == 1


def test_capability_mismatch_is_rejected_and_cancelled(
    settings: RecsInput, rpc_server: FakeRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = RecsConnection(settings)
    info = connection.inspect()
    rpc_server.sample_rate = 44_100
    with pytest.raises(ValueError, match='does not match'):
        connection.subscribe(info)
    assert rpc_server.commands[-1] == 'unsubscribe_audio'


def test_receiver_releases_capability_and_counts_known_gaps_only(
    settings: RecsInput,
    rpc_server: FakeRpc,
    samples: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
) -> None:
    receiver = RecsReceiver(settings)
    client = mock.Mock()
    pcm = samples[:960].tobytes()
    client.receive.side_effect = [
        (
            AudioHeader(type='audio', frames=960, start_frame=0, offered_at=clock[0]),
            pcm,
        ),
        (
            AudioHeader(
                type='audio',
                frames=960,
                start_frame=1920,
                offered_at=clock[0],
                input_status='input overflow',
            ),
            pcm,
        ),
        EOFError(),
    ]
    monkeypatch.setattr(recs_input, 'AudioClient', mock.Mock(return_value=client))
    with pytest.raises(EOFError):
        receiver.receive(receiver.connection.inspect())
    assert (
        receiver.dropped_frames == 2880
    )  # Exact gap + the undelivered queue, no invented overflow length.
    assert not receiver.received
    assert not receiver.connected
    client.close.assert_called_once()
    assert rpc_server.commands[-1] == 'unsubscribe_audio'


def feed(audio: RecsAudio, block: np.ndarray, now: float, frame: int) -> None:
    with audio.receiver.lock:
        audio.receiver.connected = True
        audio.receiver.intentional = False
        audio.receiver.sample_rate = 48_000
        audio.receiver.accept_packet(
            AudioHeader(
                type='audio', frames=len(block), start_frame=frame, offered_at=now
            ),
            block.tobytes(),
        )


@pytest.mark.parametrize(
    'mode,gain', [('normal', 1.0), ('muted', 0.0), ('closing', 0.5)]
)
def test_recs_pcm_uses_existing_mute_gain_and_metering(
    settings: RecsInput,
    clock: list[float],
    samples: np.ndarray,
    tmp_path: Path,
    mode: str,
    gain: float,
) -> None:
    state = RuntimeState()
    state.set_muted(mode == 'muted')
    closing = mock.Mock(spec=ClosingSequence)
    closing.audio_gain.return_value = gain
    with (tmp_path / 'received.pcm').open('w+b', buffering=0) as output:
        audio = RecsAudio(settings, 48_000, output, state, closing)
        try:
            for start in range(0, len(samples), 960):
                feed(audio, samples[start : start + 960], clock[0], start)
                audio.update()
                clock[0] += 0.02
            output.seek(0)
            result = np.frombuffer(output.read(), dtype='<f4').reshape(-1, 2)
            np.testing.assert_array_equal(result, samples * gain)
            write_wav(tmp_path / 'received.wav', result)
            assert state.snapshot()['audio_frames'] == 48_000
            assert state.snapshot()['audio_input']['connected']
            assert state.snapshot()['left_level_db'] < 0
        finally:
            audio.close_capture()


def test_receiver_reconnects_and_shutdown_interrupts_blocked_reader(
    settings: RecsInput,
    rpc_server: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = RecsReceiver(settings)
    first_closed = threading.Event()
    second_reading = threading.Event()
    packets: queue.Queue[None] = queue.Queue()
    first = mock.Mock()
    first.receive.side_effect = EOFError
    first.close.side_effect = first_closed.set
    second = mock.Mock()

    def receive() -> None:
        second_reading.set()
        packets.get(timeout=5)
        raise EOFError

    second.receive.side_effect = receive
    second.close.side_effect = lambda: packets.put(None)
    factory = mock.Mock(side_effect=[first, second])
    monkeypatch.setattr(recs_input, 'AudioClient', factory)
    receiver.start()
    try:
        assert second_reading.wait(5)
        assert first_closed.is_set()
        assert factory.call_args_list[0].args[0].subscription_id == 'offer-1'
        assert factory.call_args_list[1].args[0].subscription_id == 'offer-2'
    finally:
        receiver.close()
    assert not receiver.thread.is_alive()
    assert not receiver.connected
    assert not receiver.received
    assert rpc_server.commands.count('unsubscribe_audio') == 2


def test_slow_encoder_does_not_replay_stale_audio(
    settings: RecsInput,
    clock: list[float],
    samples: np.ndarray,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (tmp_path / 'fresh.pcm').open('w+b', buffering=0) as output:
        audio = RecsAudio(settings, 48_000, output, RuntimeState())
        try:
            with monkeypatch.context() as blocked:
                blocked.setattr(
                    recs_input.os, 'write', mock.Mock(side_effect=BlockingIOError)
                )
                for start in range(0, len(samples), 960):
                    feed(audio, -samples[start : start + 960], clock[0], start)
                    audio.update()
                    clock[0] += 0.02
                assert len(audio.receiver.received) <= 5
            clock[0] += 0.3
            for start in range(0, len(samples), 960):
                feed(audio, samples[start : start + 960], clock[0], start)
                audio.update()
                clock[0] += 0.02
            output.seek(0)
            result = np.frombuffer(output.read(), dtype='<f4').reshape(-1, 2)
            np.testing.assert_array_equal(result, samples)
            write_wav(tmp_path / 'fresh.wav', result)
        finally:
            audio.close_capture()


def test_unexpected_outage_sends_timed_silence_then_stops_at_30_seconds(
    settings: RecsInput, clock: list[float], tmp_path: Path
) -> None:
    state = RuntimeState()
    with (tmp_path / 'silence.pcm').open('w+b', buffering=0) as output:
        audio = RecsAudio(settings, 48_000, output, state)
        try:
            for step in range(100):
                clock[0] = 100 + step * 0.0101
                audio.update()
            output.seek(0)
            result = np.frombuffer(output.read(), dtype='<f4').reshape(-1, 2)
            assert len(result) == 48_000
            assert np.all(result == 0)
            write_wav(tmp_path / 'outage.wav', result)
            clock[0] = 129.99
            audio.update()
            clock[0] = 130.0
            with pytest.raises(RecsInputLost, match='30 seconds'):
                audio.update()
            assert state.snapshot()['audio_error']
        finally:
            audio.close_capture()


def test_intentional_pause_does_not_consume_outage_budget(
    settings: RecsInput, clock: list[float]
) -> None:
    audio = RecsAudio(settings, 48_000, None, RuntimeState())
    try:
        audio.receiver.lost('recs recording is paused', intentional=True)
        clock[0] = 500
        audio.update()
        audio.receiver.lost('recs unavailable', intentional=False)
        audio.update()
        clock[0] = 529.99
        audio.update()
        clock[0] = 530
        with pytest.raises(RecsInputLost):
            audio.update()
    finally:
        audio.close_capture()


def test_receiver_queue_is_bounded_and_encoder_replacement_discards_old_audio(
    settings: RecsInput, clock: list[float], samples: np.ndarray, tmp_path: Path
) -> None:
    audio = RecsAudio(settings, 48_000, None, RuntimeState())
    try:
        for start in range(0, len(samples), 960):
            feed(audio, samples[start : start + 960], clock[0], start)
        assert len(audio.receiver.received) == 5
        assert audio.receiver.dropped_frames == 43_200
        with (tmp_path / 'new-encoder.pcm').open('w+b', buffering=0) as output:
            audio.pending = b'old partial bytes'
            audio.attach_output(output)
            assert audio.pending == b''
            assert not audio.receiver.received
    finally:
        audio.close_capture()


def test_privacy_loss_pads_partial_frame_without_replaying_old_pcm(
    settings: RecsInput, clock: list[float], samples: np.ndarray, tmp_path: Path
) -> None:
    with (tmp_path / 'aligned.pcm').open('w+b', buffering=0) as output:
        audio = RecsAudio(settings, 48_000, output, RuntimeState())
        try:
            output.write(b'\0' * 3)  # Prefix of one stereo frame already in FFmpeg.
            audio.pending = b'OLD!!'
            audio.receiver.lost('mic muted', intentional=True)
            audio.update()
            for start in range(0, len(samples), 960):
                feed(audio, samples[start : start + 960], clock[0], start)
                audio.update()
                clock[0] += 0.02
            output.seek(0)
            result = np.frombuffer(output.read(), dtype='<f4').reshape(-1, 2)
            np.testing.assert_array_equal(result[0], [0, 0])
            np.testing.assert_array_equal(result[1:], samples)
            write_wav(tmp_path / 'aligned.wav', result)
        finally:
            audio.close_capture()


def test_rate_change_requires_new_encoder_before_new_pcm(
    settings: RecsInput, clock: list[float], tmp_path: Path, config: Streamo
) -> None:
    with (tmp_path / 'format.pcm').open('w+b', buffering=0) as output:
        audio = RecsAudio(settings, 48_000, output, RuntimeState())
        try:
            audio.receiver.sample_rate = 44_100
            audio.receiver.epoch += 1
            with pytest.raises(AudioFormatChanged):
                audio.update()
            command = streamer.ffmpeg_command(
                config, input_sample_rate=audio.sample_rate
            )
            assert command[10] == '44100'
            output.seek(0)
            assert output.read() == b''
        finally:
            audio.close_capture()


def test_stream_uses_recs_only_and_input_loss_does_not_enter_publish_retry(
    config: Streamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamo.control import ControlController

    audio = mock.Mock(spec=RecsAudio)
    audio.receiver = mock.Mock()
    monkeypatch.setattr(streamer, 'RecsAudio', mock.Mock(return_value=audio))
    monkeypatch.setattr(
        streamer,
        'AudioCapture',
        mock.Mock(side_effect=AssertionError('opened hardware')),
    )
    attempt = mock.Mock(
        side_effect=[
            AudioFormatChanged(),
            RecsInputLost('recs input unavailable for 30 seconds'),
        ]
    )
    monkeypatch.setattr(streamer, 'run_attempt', attempt)
    controller = ControlController(RuntimeState())
    service = mock.Mock(service=config.streaming_service, capabilities=[])
    result = streamer.stream(config, controller, service, initial_image_paths=set())
    assert result == 1
    assert attempt.call_count == 2
    assert controller.state.snapshot()['state'] == 'failed'
    assert '30 seconds' in controller.state.snapshot()['publish_error']
    audio.receiver.start.assert_called_once()
    audio.close_capture.assert_called_once()


def test_preview_pipe_survives_encoder_replacement_for_native_rate_change(
    config: Streamo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from streamo.control import ControlController

    audio = mock.Mock(sample_rate=48_000)
    audio.update.side_effect = AudioFormatChanged
    encoder = mock.Mock(stderr=None)
    encoder.poll.return_value = None
    player = mock.Mock()
    player.poll.return_value = None
    monkeypatch.setattr(streamer.subprocess, 'Popen', mock.Mock(return_value=encoder))
    monkeypatch.setattr(streamer, 'close_encoder', mock.Mock())
    controller = ControlController(RuntimeState())
    for rate in (48_000, 44_100):
        audio.sample_rate = rate
        with pytest.raises(AudioFormatChanged):
            streamer.run_attempt(
                config,
                controller,
                mock.Mock(),
                audio,
                None,
                None,
                player,
                None,
                mock.Mock(),
            )
    player.stdin.close.assert_not_called()
    assert audio.attach_output.call_count == 4
