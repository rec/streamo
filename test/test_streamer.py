from pathlib import Path
from unittest import mock
from urllib.parse import quote, quote_plus, unquote_plus

import pytest

from streamo import composition, provider_config, providers, streamer
from streamo.composition import (
    LOCAL_DISPLAY_URL,
    ffmpeg_command,
)
from streamo.config import Streamo
from streamo.control import ControlController
from streamo.provider_config import (
    AudioEncoding,
    EncodingProfile,
    IcecastIngest,
    IcecastService,
    RtmpIngest,
    TwitchService,
    VideoEncoding,
)
from streamo.providers import ingest_output
from streamo.runtime import RuntimeState
from streamo.streamer import (
    LocalDisplayController,
    drm_connected,
    ffplay_command,
    local_ffplay_command,
    redacted_ffmpeg_command,
)


def _config() -> Streamo:
    return Streamo(
        live_overlays=False,
        device_name='X18',
        channel=2,
        video=Path('visual-bed.mp4'),
        streaming_service=TwitchService(
            service='twitch',
            ingest=RtmpIngest(
                protocol='rtmps',
                server_url='rtmps://live.twitch.tv/app',
                stream_key='key',
            ),
            encoding=EncodingProfile(
                container='flv',
                audio=AudioEncoding(
                    codec='aac', bitrate='160k', sample_rate=48_000, channels=2
                ),
                video=VideoEncoding(
                    codec='h264',
                    bitrate='150k',
                    resolution='640x360',
                    frame_rate=10,
                    keyframe_interval=2,
                ),
            ),
        ),
    )


def test_ffmpeg_command_streams_audio_pipe_and_video_loop() -> None:
    command = ffmpeg_command(_config())

    assert command[:9] == [
        'ffmpeg',
        '-hide_banner',
        '-loglevel',
        'warning',
        '-nostats',
        '-progress',
        'pipe:2',
        '-f',
        'f32le',
    ]
    assert command[9:13] == [
        '-ar',
        '48000',
        '-ac',
        '2',
    ]
    assert 'visual-bed.mp4' in command
    assert '-stream_loop' in command

    assert '-filter_complex' not in command
    assert command[-3:-1] == ['-f', 'tee']
    assert command[-1] == (
        '[f=flv]rtmps\\://live.twitch.tv/app/key|'
        '[f=mpegts]udp\\://127.0.0.1\\:23000?pkt_size=1316'
    )
    assert command.count('-c:v') == 1


def test_overlay_preserves_encoded_base_resolution_and_frame_rate() -> None:
    config = _config()
    service = config.streaming_service.model_copy(
        update={
            'encoding': config.streaming_service.encoding.model_copy(
                update={
                    'video': VideoEncoding(
                        codec='h264',
                        bitrate='2500k',
                        resolution='1920x1080',
                        frame_rate=30,
                        keyframe_interval=2,
                    ),
                }
            ),
        }
    )
    config = config.model_copy(update={'streaming_service': service})
    graph = composition.overlay_filter(config)
    assert '[1:v]scale=1920:1080,fps=30' in graph


def test_ffmpeg_command_can_disable_local_display() -> None:
    command = ffmpeg_command(_config().model_copy(update={'local_display': False}))

    assert command[-3:] == ['-f', 'flv', 'rtmps://live.twitch.tv/app/key']


def test_live_overlay_input_uses_output_resolution() -> None:
    config = _config().model_copy(
        update={'live_overlays': True, 'video_resolution': '1280x720'}
    )
    command = ffmpeg_command(config, image_pipe=7)
    assert command[command.index('-video_size') + 1] == '640x360'
    assert 'pipe:7' in command
    assert '[base][live]overlay=0:0' in command[command.index('-filter_complex') + 1]
    with pytest.raises(ValueError, match='overlay pipe'):
        ffmpeg_command(config)


def test_disabled_overlays_omit_title_and_photo_inputs(tmp_path: Path) -> None:
    config = _config().model_copy(
        update={'title_card': tmp_path / 'title.png', 'image_interval': 60}
    )
    command = ffmpeg_command(config)
    assert '-filter_complex' not in command
    assert 'rawvideo' not in command
    assert str(config.title_card) not in command


def test_ffmpeg_command_previews_nut_on_stdout() -> None:
    command = ffmpeg_command(_config(), preview=True)

    assert command[-3:] == ['-f', 'nut', 'pipe:1']
    assert ffplay_command()[-3:] == ['-f', 'nut', 'pipe:0']
    assert 'tee' not in command


def test_ffmpeg_command_omits_video_for_icecast() -> None:
    config = Streamo(
        device_name='X18',
        channel=2,
        streaming_service=IcecastService(
            service='icecast',
            ingest=IcecastIngest(
                protocol='icecast',
                server_url='icecast://radio.example.test:8000',
                mountpoint='/live',
                password='secret',
            ),
            encoding=EncodingProfile(
                container='mp3',
                audio=AudioEncoding(
                    codec='mp3', bitrate='160k', sample_rate=48_000, channels=2
                ),
            ),
        ),
    )

    command = ffmpeg_command(config)

    assert config.video is None
    assert '-c:v' not in command
    assert '-map' in command
    assert '0:a:0' in command
    assert 'icecast://source:secret@radio.example.test:8000/live' == command[-1]


def test_process_diagnostic_command_redacts_output_secret() -> None:
    config = _config()
    output = ingest_output(config.streaming_service)

    local_output = composition.local_display_output(config, output)
    command = redacted_ffmpeg_command(
        ffmpeg_command(config, output=local_output), local_output
    )

    assert 'key' not in ' '.join(command)
    assert '[REDACTED]' in command[-1]
    assert 'udp\\://127.0.0.1\\:23000?pkt_size=1316' in command[-1]


def test_preview_is_cleaned_up_when_command_building_fails() -> None:
    player = mock.Mock()
    with (
        mock.patch.object(streamer.subprocess, 'Popen', return_value=player),
        mock.patch.object(streamer, 'ffmpeg_command', side_effect=ValueError('bad')),
        mock.patch.object(streamer.process, 'terminate') as terminate,
        pytest.raises(ValueError, match='bad'),
    ):
        streamer.stream(
            _config(),
            ControlController(RuntimeState()),
            mock.Mock(),
            initial_image_paths=set(),
            preview=True,
        )
    terminate.assert_called_once_with(player)
    player.stdin.close.assert_called_once()


def test_service_cleanup_runs_if_preparation_fails() -> None:
    service = mock.Mock()
    service.prepare.side_effect = ValueError('bad preparation')
    with pytest.raises(ValueError, match='bad preparation'):
        streamer.stream(
            _config(),
            ControlController(RuntimeState()),
            service,
            initial_image_paths=set(),
        )
    service.finish.assert_called_once()


def test_local_ffplay_command_uses_fullscreen_silent_mpegts() -> None:
    command = local_ffplay_command()

    assert '-fs' in command
    assert '-an' in command
    assert command[-3:] == ['-f', 'mpegts', LOCAL_DISPLAY_URL]


@pytest.mark.parametrize(
    ('ingest', 'container'),
    [
        (
            {
                'protocol': 'rtmp',
                'server_url': 'rtmp://ingest.test/app',
                'stream_key': 'private/key value',
            },
            'flv',
        ),
        (
            {
                'protocol': 'rtmps',
                'server_url': 'rtmps://ingest.test/app',
                'stream_key': 'private/key value',
            },
            'flv',
        ),
        (
            {
                'protocol': 'srt',
                'url': 'srt://ingest.test:9000',
                'passphrase': 'private/key value',
            },
            'mpegts',
        ),
        (
            {
                'protocol': 'hls',
                'upload_url': 'https://ingest.test/{stream_key}/live.m3u8',
                'stream_key': 'private/key value',
                'segment_duration': 2,
            },
            'mpegts',
        ),
        (
            {
                'protocol': 'icecast',
                'server_url': 'icecast://ingest.test:8000',
                'mountpoint': '/live',
                'password': 'private/key value',
            },
            'adts',
        ),
    ],
)
def test_failure_logs_hide_ingest_urls_and_secrets(
    ingest: dict[str, object], container: str, caplog: pytest.LogCaptureFixture
) -> None:
    service = provider_config.CustomService.model_validate(
        {
            'service': 'custom',
            'ingest': ingest,
            'encoding': {
                'container': container,
                'audio': {
                    'codec': 'aac',
                    'bitrate': '160k',
                    'sample_rate': 48000,
                    'channels': 2,
                },
            },
        }
    )
    output = composition.local_display_output(_config(), ingest_output(service))
    url = output.destinations[0].url
    secret = 'private/key value'
    variants = [
        url,
        unquote_plus(url),
        providers.tee_escape(url),
        secret,
        quote(secret, safe=''),
        quote_plus(secret),
    ]
    stderr = '\n'.join(variants) + '\nConnection refused; frame=10'
    if ingest['protocol'] == 'hls':
        stderr += '\n' + url.replace('live.m3u8', 'live0001.ts')

    streamer.process.report_failed_command(
        ['ffmpeg', *output.redacted_arguments()],
        None,
        streamer.redacted_ffmpeg_stderr(stderr, output, service),
    )

    assert all(v not in caplog.text for v in variants)
    assert 'Connection refused; frame=10' in caplog.text
    assert '[REDACTED]' in caplog.text
    assert '127.0.0.1' in caplog.text


def test_drm_connected_reads_any_connected_connector(tmp_path: Path) -> None:
    disconnected = tmp_path / 'card0-HDMI-A-1' / 'status'
    connected = tmp_path / 'card0-HDMI-A-2' / 'status'
    disconnected.parent.mkdir()
    connected.parent.mkdir()
    disconnected.write_text('disconnected\n')
    connected.write_text('connected\n')

    assert drm_connected(tmp_path)


def test_local_display_follows_connector_transitions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = tmp_path / 'card0-HDMI-A-1' / 'status'
    status.parent.mkdir()
    status.write_text('disconnected\n')
    players: list[FakePlayer] = []
    calls: list[dict[str, object]] = []

    def popen(command: list[str], **kwargs: object) -> FakePlayer:
        calls.append({'command': command, **kwargs})
        player = FakePlayer()
        players.append(player)
        return player

    monkeypatch.setattr(streamer.subprocess, 'Popen', popen)
    display = LocalDisplayController(tmp_path)

    display.update()
    status.write_text('connected\n')
    display.update()
    display.update()
    status.write_text('disconnected\n')
    display.update()
    status.write_text('connected\n')
    display.update()
    display.close()

    assert len(players) == 2
    assert players[0].terminated
    assert players[1].terminated
    assert calls[0]['command'] == local_ffplay_command()
    assert calls[0]['env'] == {**streamer.os.environ, 'SDL_VIDEODRIVER': 'KMSDRM'}


def test_local_display_restarts_after_delay_without_connector_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = tmp_path / 'card0-HDMI-A-1' / 'status'
    status.parent.mkdir()
    status.write_text('connected\n')
    players: list[FakePlayer] = []
    now = 100.0
    monkeypatch.setattr(streamer.time, 'monotonic', lambda: now)
    monkeypatch.setattr(
        streamer.subprocess,
        'Popen',
        lambda *args, **kwargs: players.append(FakePlayer()) or players[-1],
    )
    display = LocalDisplayController(tmp_path)

    display.update()
    players[0].returncode = 1
    display.update()
    assert len(players) == 1
    now = 104.99
    display.update()
    assert len(players) == 1
    now = 105.0
    display.update()

    assert len(players) == 2


def test_local_display_ignores_player_launch_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status = tmp_path / 'card0-HDMI-A-1' / 'status'
    status.parent.mkdir()
    status.write_text('connected\n')
    launches = 0

    def popen(*args: object, **kwargs: object) -> None:
        nonlocal launches
        launches += 1
        raise OSError('no DRM device')

    monkeypatch.setattr(streamer.subprocess, 'Popen', popen)
    display = LocalDisplayController(tmp_path)

    display.update()
    status.write_text('disconnected\n')
    display.update()
    status.write_text('connected\n')
    display.update()

    assert launches == 2


class FakePlayer:
    def __init__(self) -> None:
        self.stderr = None
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0
