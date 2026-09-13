from pathlib import Path

import pytest

from scripts import loop_videos
from scripts.loop_videos import ffmpeg_command, looped_path


def test_looped_path_adds_looped_before_suffix() -> None:
    assert looped_path(Path('movie.mp4')) == Path('movie-looped.mp4')
    assert looped_path(Path('some.dir/movie.test.mov')) == Path(
        'some.dir/movie.test-looped.mov'
    )


def test_ffmpeg_command_trims_duplicate_endpoint_frames() -> None:
    command = ffmpeg_command(Path('movie.mp4'), Path('movie-looped.mp4'), 10)

    assert '-y' in command
    assert '-n' not in command
    assert 'movie.mp4' in command
    assert 'movie-looped.mp4' in command
    filters = command[command.index('-filter_complex') + 1]
    assert 'reverse,trim=start_frame=1:end_frame=9' in filters
    assert '[fwd][rev]concat=n=2:v=1:a=0[out]' in filters


def test_loop_video_rejects_too_few_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / 'movie.mp4'
    video.touch()
    monkeypatch.setattr(loop_videos, 'count_frames', lambda path: 2)

    with pytest.raises(SystemExit, match='fewer than 3 frames'):
        loop_videos.loop_video(video)
