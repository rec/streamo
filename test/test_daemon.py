import json
from pathlib import Path
from unittest import mock

from reccy.services.models import StatusResult

from streamo import daemon
from streamo.config import Streamo


def test_install_creates_daemon_service_with_absolute_config_path() -> None:
    config = Path("private/config.json")
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
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "device_name": "X18",
                "channel": 1,
                "video": "visual-bed.mp4",
                "twitch_key": "unused",
            }
        )
    )
    with mock.patch.object(Streamo, "run", autospec=True, return_value=0) as run:
        result = daemon.run(daemon.DaemonOptions(action="preview", config=config))

    assert result == 0
    assert run.call_args.kwargs == {"preview": True}
