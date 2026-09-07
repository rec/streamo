import io
from pathlib import Path

import numpy as np
import pytest

from streamo.config import Streamo
from streamo.control import RuntimeState
from streamo.streamer import (
    _audio_callback,
    ffmpeg_command,
    ffplay_command,
    select_stereo_pair,
    title_filter,
    video_size,
)


def _config() -> Streamo:
    return Streamo(
        device_name="X18",
        channel=2,
        video=Path("visual-bed.mp4"),
        twitch_key="key",
    )


def test_ffmpeg_command_streams_audio_pipe_and_video_loop() -> None:
    command = ffmpeg_command(_config())

    assert command[:9] == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:2",
        "-f",
        "f32le",
    ]
    assert command[9:13] == [
        "-ar",
        "48000",
        "-ac",
        "2",
    ]
    assert "visual-bed.mp4" in command
    assert "-stream_loop" in command
    assert "-filter_complex" not in command
    assert command[-1] == "rtmp://live.twitch.tv/app/key"


def test_ffmpeg_command_overlays_title_card(tmp_path: Path) -> None:
    title = tmp_path / "title.png"
    title.touch()
    config = _config().model_copy(update={"title_card": title})

    command = ffmpeg_command(config)
    graph = command[command.index("-filter_complex") + 1]

    assert title.as_posix() in command
    assert "color=c=black@0.0:s=640x360:r=10:d=172.000000" in command
    assert command[command.index("-map") + 1] == "[video]"
    assert "[base][title_loop]overlay=(W-w)/2:(H-h)/2" in graph
    assert "fade=t=in:st=0:d=2.000000:alpha=1" in graph
    assert "fade=t=out:st=6.000000:d=2.000000:alpha=1" in graph
    assert "loop=loop=-1:size=1800:start=0" in graph


def test_ffmpeg_command_overlays_live_image_pipe() -> None:
    config = _config().model_copy(
        update={
            "image_interval": 60,
            "image_duration": 10,
            "image_fade": 3,
            "video_frame_rate": 24,
            "video_resolution": "1280x720",
        }
    )

    command = ffmpeg_command(config, image_pipe=7)
    graph = command[command.index("-filter_complex") + 1]

    assert command[command.index("rawvideo") - 1 :][0:10] == [
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgba",
        "-video_size",
        "1280x720",
        "-framerate",
        "24",
        "-i",
        "pipe:7",
    ]
    assert "[2:v]setpts=PTS-STARTPTS[image_live]" in graph
    assert "[base][image_live]overlay=(W-w)/2:(H-h)/2" in graph


def test_ffmpeg_command_places_live_images_after_title(tmp_path: Path) -> None:
    title = tmp_path / "title.png"
    title.touch()
    config = _config().model_copy(update={"title_card": title, "image_interval": 60})

    command = ffmpeg_command(config, image_pipe=8)
    graph = command[command.index("-filter_complex") + 1]

    assert "[base][title_loop]overlay=(W-w)/2:(H-h)/2:eof_action=repeat[base1]" in graph
    assert "[base1][image_live]overlay=(W-w)/2:(H-h)/2" in graph


def test_ffmpeg_command_previews_nut_on_stdout() -> None:
    command = ffmpeg_command(_config(), preview=True)

    assert command[-3:] == ["-f", "nut", "pipe:1"]
    assert ffplay_command()[-3:] == ["-f", "nut", "pipe:0"]


def test_video_size_parses_resolution() -> None:
    assert video_size(_config()) == (640, 360)


def test_title_filter_uses_configured_timing(tmp_path: Path) -> None:
    title = tmp_path / "title.png"
    title.touch()
    config = _config().model_copy(
        update={
            "title_card": title,
            "title_interval": 60,
            "title_duration": 10,
            "title_fade": 3,
            "video_frame_rate": 24,
            "video_resolution": "1280x720",
        }
    )

    graph = title_filter(config)

    assert "scale=1280:720" in graph
    assert "fps=24" in graph
    assert "fade=t=out:st=7.000000:d=3.000000:alpha=1" in graph
    assert "loop=loop=-1:size=1440:start=0" in graph


def test_select_stereo_pair_uses_one_based_channel_number() -> None:
    config = _config()
    data = np.array([[1, 2, 3, 4], [5, 6, 7, 8]], dtype=np.float32)

    assert select_stereo_pair(config, data).tolist() == [[2, 3], [6, 7]]


def test_select_stereo_pair_rejects_missing_second_channel() -> None:
    with pytest.raises(ValueError, match="requires a stereo pair"):
        select_stereo_pair(_config(), np.zeros((2, 2), dtype=np.float32))


def test_audio_callback_writes_silence_when_muted() -> None:
    state = RuntimeState()
    state.set_muted(True)
    process = FakeProcess()
    callback = _audio_callback(_config(), process, state)

    callback(np.array([[1, -1, 0], [0.5, -0.5, 0]], dtype=np.float32), 2, None, None)

    written = np.frombuffer(process.stdin.getvalue(), dtype=np.float32).reshape((2, 2))
    assert written.tolist() == [[0, 0], [0, 0]]
    assert state.snapshot()["audio_frames"] == 2


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()
