import tomllib
from pathlib import Path
from unittest import mock

import pytest
from reccy.services.models import StatusResult

from streamo import daemon
from streamo.config import Streamo


def test_install_creates_daemon_service_with_absolute_config_path() -> None:
    config = Path("private/config.toml")
    with (
        mock.patch.object(
            Streamo,
            "install_service",
            return_value=StatusResult(installed=True, running=True),
        ) as install_service,
        mock.patch("streamo.daemon.print_service_status"),
    ):
        result = daemon.run(daemon.DaemonOptions(action="install", config=config))

    assert result == 0
    assert install_service.call_args.args == (
        [
            "daemon",
            "run",
            "--config",
            str(config.resolve()),
        ],
    )


def test_preview_runs_config_without_twitch_output(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
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
    with mock.patch.object(Streamo, "run", autospec=True, return_value=0) as run:
        result = daemon.run(daemon.DaemonOptions(action="preview", config=config))

    assert result == 0
    assert run.call_args.kwargs == {"preview": True}


def test_example_configs_parse() -> None:
    examples = Path(__file__).parents[1] / "examples"

    services = [
        daemon.load_config(path).streaming_service.service
        for path in sorted(examples.glob("*.toml"))
    ]

    assert services == ["custom", "icecast", "twitch"]


def test_json_configuration_is_not_accepted(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"device_name": "X18"}')

    with pytest.raises(tomllib.TOMLDecodeError):
        daemon.load_config(config)
