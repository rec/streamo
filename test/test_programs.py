import logging
import subprocess
import sys

import pytest
from reccy.runtime.process import run_silent

from streamo.control import RuntimeState
from streamo.programs import parse_bitrate, update_bitrate


def test_run_silent_hides_successful_output(capsys: pytest.CaptureFixture[str]) -> None:
    run_silent(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ]
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_silent_logs_failed_output(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)

    with pytest.raises(subprocess.CalledProcessError):
        run_silent(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(2)",
            ]
        )

    assert "Command failed:" in caplog.text
    assert "stdout:" in caplog.text
    assert "out" in caplog.text
    assert "stderr:" in caplog.text
    assert "err" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("124.8kbits/s", 124.8),
        ("300000bits/s", 300.0),
        ("1.2Mbits/s", 1200.0),
        ("N/A", None),
    ],
)
def test_parse_bitrate(value: str, expected: float | None) -> None:
    assert parse_bitrate(value) == expected


def test_update_bitrate_stores_ffmpeg_progress_value() -> None:
    state = RuntimeState()

    update_bitrate(state, "bitrate= 250.5kbits/s\n")

    assert state.snapshot()["output_bitrate_kbps"] == 250.5
