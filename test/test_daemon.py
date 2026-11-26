import hashlib
import tomllib
from pathlib import Path
from unittest import mock

import pytest
from reccy.services.models import StatusResult

from streamo import daemon
from streamo.config import Streamo
from streamo.managed import LaunchPermit


@pytest.mark.parametrize(
    ('action', 'installed', 'running', 'expected'),
    [
        ('stop', True, False, 0),
        ('stop', True, True, 1),
        ('uninstall', False, False, 0),
        ('uninstall', True, False, 1),
        ('install', True, False, 0),
        ('start', True, False, 1),
    ],
)
def test_service_exit_code_matches_requested_action(
    action: str, installed: bool, running: bool, expected: int
) -> None:
    with (
        mock.patch.object(
            Streamo,
            'service_status',
            return_value=StatusResult(
                installed=installed,
                running=running,
            ),
        ),
        mock.patch.object(Streamo, f'{action}_service'),
        mock.patch('streamo.daemon.print_service_status'),
    ):
        assert (
            daemon.run(daemon.DaemonOptions.model_validate({'action': action}))
            == expected
        )


@pytest.mark.parametrize('manual', [False, True])
def test_install_creates_service_with_requested_start_policy(manual: bool) -> None:
    config = Path('private/config.toml')
    with (
        mock.patch.object(
            Streamo,
            'service_status',
            return_value=StatusResult(installed=True, running=not manual),
        ),
        mock.patch.object(Streamo, 'install_service') as install_service,
        mock.patch('streamo.daemon.print_service_status'),
    ):
        flags = ['--manual'] if manual else []
        result = daemon.main(['install', '--config', str(config), *flags])

    assert result == 0
    install_service.assert_called_once_with(
        [
            'daemon',
            'run',
            '--config',
            str(config.resolve()),
            *(['--managed'] if manual else []),
        ],
        manual=manual,
    )


def test_status_uses_reccy_service_status() -> None:
    with (
        mock.patch.object(
            Streamo,
            'service_status',
            return_value=StatusResult(installed=True, running=True),
        ) as service_status,
        mock.patch('streamo.daemon.print_service_status'),
    ):
        assert daemon.run(daemon.DaemonOptions(action='status')) == 0
    service_status.assert_called_once_with()


def test_preview_runs_config_without_twitch_output(tmp_path: Path) -> None:
    config = tmp_path / 'config.toml'
    config.write_text(
        """
device_name = "X18"
channel = 1
video = "visual-bed.mp4"

[streaming_service]
service = "twitch"

[streaming_service.ingest]
protocol = "rtmps"
server_url = "rtmps://live.twitch.tv/app"
stream_key = "unused"

[streaming_service.encoding]
container = "flv"

[streaming_service.encoding.audio]
codec = "aac"
bitrate = "160k"
sample_rate = 48000
channels = 2

[streaming_service.encoding.video]
codec = "h264"
bitrate = "150k"
resolution = "640x360"
frame_rate = 10
keyframe_interval = 2
"""
    )
    with mock.patch.object(Streamo, 'run', autospec=True, return_value=0) as run:
        result = daemon.run(daemon.DaemonOptions(action='preview', config=config))

    assert result == 0
    assert run.call_args.kwargs == {'preview': True, 'permit': None}


def test_managed_daemon_rejects_configuration_changed_after_readiness(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'streamo.toml'
    config.write_text('channel = 1\n')
    permit = LaunchPermit(
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        expires_at=100,
    )
    config.write_text('channel = 2\n')
    with (
        mock.patch.object(daemon, 'read_launch_permit', return_value=permit),
        mock.patch.object(Streamo, 'run') as run,
        pytest.raises(SystemExit, match='changed after readiness'),
    ):
        daemon.run(daemon.DaemonOptions(action='run', config=config, managed=True))
    run.assert_not_called()


def test_example_configs_parse() -> None:
    examples = Path(__file__).parents[1] / 'examples'

    services = [
        daemon.load_config(path).streaming_service.service
        for path in sorted(examples.glob('*.toml'))
    ]

    assert services == ['custom', 'icecast', 'twitch']


def test_image_directories_resolve_from_config_file(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    path.write_text(
        """
device_name = "X18"
channel = 1
video = "visual-bed.mp4"
image_dir = ["images/live", "images/archive"]
image_dir_weights = "3,1"

[streaming_service]
service = "custom"

[streaming_service.ingest]
protocol = "rtmps"
server_url = "rtmps://ingest.example.test/live"
stream_key = "unused"

[streaming_service.encoding]
container = "flv"

[streaming_service.encoding.audio]
codec = "aac"
bitrate = "160k"
sample_rate = 48000
channels = 2

[streaming_service.encoding.video]
codec = "h264"
bitrate = "150k"
resolution = "640x360"
frame_rate = 10
keyframe_interval = 2
"""
    )

    config = daemon.load_config(path)

    assert config.image_dir == [
        tmp_path / 'images/live',
        tmp_path / 'images/archive',
    ]
    assert config.resolved_image_dir_weights == [3, 1]


def test_json_configuration_is_not_accepted(tmp_path: Path) -> None:
    config = tmp_path / 'config.json'
    config.write_text('{"device_name": "X18"}')

    with pytest.raises(tomllib.TOMLDecodeError):
        daemon.load_config(config)


def test_startup_names_missing_configuration_file(tmp_path: Path) -> None:
    config = tmp_path / 'missing.toml'
    with pytest.raises(SystemExit) as raised:
        daemon.run(daemon.DaemonOptions(config=config))
    assert str(config) in str(raised.value)
    assert 'configuration file does not exist' in str(raised.value)


def test_startup_explains_missing_twitch_key(tmp_path: Path) -> None:
    config = tmp_path / 'config.toml'
    config.write_text('video = "bed.mp4"\n')
    with pytest.raises(SystemExit) as raised:
        daemon.run(daemon.DaemonOptions(config=config))
    message = str(raised.value)
    assert str(config) in message
    assert '[streaming_service.ingest]' in message
    assert 'stream_key = "YOUR_KEY"' in message
    assert 'streaming_service.twitch' not in message
