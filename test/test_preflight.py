import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import sounddevice
from reccy.protocol import rpc

from streamo import preflight
from streamo.__main__ import main
from streamo.config import Streamo
from streamo.provider_config import CustomService, VideoEncoding


@pytest.fixture
def config(tmp_path: Path) -> Streamo:
    return Streamo(
        device_name='Mixer',
        channel=17,
        image_dir=tmp_path,
        streaming_service=CustomService.model_validate(
            {
                'service': 'custom',
                'ingest': {
                    'protocol': 'rtmps',
                    'server_url': 'rtmps://example.test/live',
                    'stream_key': 'must-not-appear',
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


def test_default_checks_never_open_input_or_initialize_provider(
    config: Streamo,
) -> None:
    with (
        mock.patch.object(
            preflight.sounddevice,
            'query_devices',
            return_value={'name': 'Mixer', 'max_input_channels': 18},
        ),
        mock.patch.object(preflight.sounddevice, 'check_input_settings'),
        mock.patch.object(
            preflight.sounddevice,
            'InputStream',
            side_effect=AssertionError('opened input'),
        ),
        mock.patch.object(
            preflight, 'adapter_for', side_effect=AssertionError('initialized provider')
        ),
        mock.patch.object(
            preflight.subprocess,
            'run',
            return_value=subprocess.CompletedProcess(
                [], 0, ' A..... aac AAC encoder\n'
            ),
        ),
    ):
        report = preflight.check(config)
    assert report.ok
    assert next(c for c in report.checks if c.name == 'audio_open').status == 'skipped'
    assert (
        next(c for c in report.checks if c.name == 'provider_access').status
        == 'skipped'
    )
    assert 'must-not-appear' not in report.model_dump_json()


def test_insufficient_channels_are_reported_before_format_probe(
    config: Streamo,
) -> None:
    with (
        mock.patch.object(
            preflight.sounddevice,
            'query_devices',
            return_value={'name': 'Mixer', 'max_input_channels': 2},
        ),
        mock.patch.object(
            preflight.sounddevice,
            'check_input_settings',
            side_effect=AssertionError('format probed'),
        ),
    ):
        checks = preflight.check_audio(config, probe_device=False)
    assert checks[0].status == 'fail'
    assert '17-18' in checks[0].detail
    assert '2 inputs' in checks[0].detail
    assert checks[1].status == 'skipped'


def test_missing_device_is_distinct_from_unsupported_format(config: Streamo) -> None:
    with mock.patch.object(
        preflight.sounddevice, 'query_devices', side_effect=ValueError('not found')
    ):
        unavailable = preflight.check_audio(config, probe_device=False)
    assert unavailable[0].name == 'audio_device'
    assert unavailable[0].status == 'fail'
    with (
        mock.patch.object(
            preflight.sounddevice,
            'query_devices',
            return_value={'name': 'Mixer', 'max_input_channels': 18},
        ),
        mock.patch.object(
            preflight.sounddevice,
            'check_input_settings',
            side_effect=sounddevice.PortAudioError('bad rate'),
        ),
    ):
        unsupported = preflight.check_audio(config, probe_device=False)
    assert unsupported[0].status == 'pass'
    assert unsupported[1].name == 'audio_format'
    assert unsupported[1].status == 'fail'
    assert '48000' in unsupported[1].detail


def test_explicit_device_probe_closes_without_starting_capture(config: Streamo) -> None:
    capture = mock.Mock()
    with (
        mock.patch.object(
            preflight.sounddevice,
            'query_devices',
            return_value={'name': 'Mixer', 'max_input_channels': 18},
        ),
        mock.patch.object(preflight.sounddevice, 'check_input_settings'),
        mock.patch.object(preflight.sounddevice, 'InputStream', return_value=capture),
    ):
        checks = preflight.check_audio(config, probe_device=True)
    assert checks[-1].status == 'pass'
    capture.close.assert_called_once()
    capture.start.assert_not_called()


def test_missing_encoder_fails_even_when_its_name_is_in_description(
    config: Streamo,
) -> None:
    with mock.patch.object(
        preflight.subprocess,
        'run',
        return_value=subprocess.CompletedProcess(
            [], 0, ' A..... libother alternative to aac\n'
        ),
    ):
        result = preflight.check_encoders(config)
    assert result.status == 'fail'
    assert result.detail == 'Missing: aac'


def test_missing_image_directory_warns_without_creating_it(
    config: Streamo, tmp_path: Path
) -> None:
    directory = tmp_path / 'not-created' / 'images'
    result = preflight.check_storage(
        config.model_copy(update={'image_dir': directory, 'image_interval': 20})
    )
    assert result.status == 'warning'
    assert not directory.parent.exists()


def test_media_without_video_stream_fails(config: Streamo, tmp_path: Path) -> None:
    encoding = config.streaming_service.encoding.model_copy(
        update={
            'video': VideoEncoding(
                codec='h264',
                bitrate='2500k',
                resolution='1280x720',
                frame_rate=30,
                keyframe_interval=2,
            )
        }
    )
    service = config.streaming_service.model_copy(update={'encoding': encoding})
    config = config.model_copy(
        update={'video': tmp_path / 'audio.mp4', 'streaming_service': service}
    )
    with mock.patch.object(
        preflight.subprocess,
        'run',
        return_value=subprocess.CompletedProcess([], 0, '{"streams": []}'),
    ):
        checks = preflight.check_media(config)
    assert checks[0].status == 'fail'
    assert checks[0].detail == 'Visual bed has no video stream'


def test_cli_and_rpc_return_same_report(
    config: Streamo, capsys: pytest.CaptureFixture[str]
) -> None:
    report = preflight.Report(
        checks=[preflight.Check(name='audio_device', status='fail', detail='No input')]
    )
    with (
        mock.patch.object(preflight, 'load_config', return_value=config),
        mock.patch.object(preflight, 'check', return_value=report),
    ):
        assert main(['preflight']) == 1
        response = config.rpc_response(rpc.Request(command='preflight'))
    assert json.loads(capsys.readouterr().out) == response


def test_remote_probe_does_not_report_secrets(config: Streamo) -> None:
    with mock.patch.object(
        preflight, 'adapter_for', side_effect=ValueError('must-not-appear')
    ):
        result = preflight.check_remote(config, probe_remote=True)
    assert result.status == 'fail'
    assert 'must-not-appear' not in result.model_dump_json()


def test_cli_missing_config_is_structured_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(['preflight', '--config', str(tmp_path / 'missing.toml')]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is False
    assert result['checks'][0]['name'] == 'configuration'
