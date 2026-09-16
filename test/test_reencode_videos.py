import subprocess as sp
from pathlib import Path

import pytest

from scripts import reencode_videos


def test_reencode_command_uses_mezzanine_settings() -> None:
    command = reencode_videos.reencode_command(
        Path('source.mov'), Path('out.mp4'), bitrate_kbps=1200
    )

    assert command == [
        'ffmpeg',
        '-hide_banner',
        '-n',
        '-i',
        'source.mov',
        '-vf',
        'scale=1280:-2,fps=30',
        '-an',
        '-c:v',
        'libx264',
        '-b:v',
        '1200k',
        '-maxrate',
        '1200k',
        '-bufsize',
        '2400k',
        '-preset',
        'slow',
        '-pix_fmt',
        'yuv420p',
        'out.mp4',
    ]


def test_reencode_files_writes_mp4_files_to_output_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / 'output'
    a = tmp_path / 'a.mov'
    b = tmp_path / 'b.mp4'
    a.write_text('a')
    b.write_text('b')
    commands: list[list[str]] = []

    def run_silent(command: list[str], *, text: bool = False) -> sp.CompletedProcess:
        commands.append(command)
        if command[0] == 'ffprobe':
            return sp.CompletedProcess(command, 0, stdout='10.0')
        Path(command[-1]).write_text('encoded')
        return sp.CompletedProcess(command, 0)

    monkeypatch.setattr(reencode_videos, 'run_silent', run_silent)

    reencode_videos.reencode_files([a, b], output)

    ffmpeg_commands = [c for c in commands if c[0] == 'ffmpeg']
    assert [Path(c[-1]).name for c in ffmpeg_commands] == ['a.mp4', 'b.mp4']
    assert (output / 'a.mp4').read_text() == 'encoded'
    assert (output / 'b.mp4').read_text() == 'encoded'


def test_reencode_files_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match='is not a file'):
        reencode_videos.reencode_files([tmp_path / 'missing.mp4'], tmp_path / 'output')


def test_target_bitrate_uses_source_size_and_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'0' * 2_000_000)
    monkeypatch.setattr(reencode_videos, 'duration', lambda video: 10.0)

    bitrate = reencode_videos.target_bitrate_kbps(
        video, max_bitrate_kbps=1200, source_ratio=0.8
    )

    assert bitrate == 1200


def test_target_bitrate_is_capped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'0' * 20_000_000)
    monkeypatch.setattr(reencode_videos, 'duration', lambda video: 10.0)

    bitrate = reencode_videos.target_bitrate_kbps(
        video, max_bitrate_kbps=1200, source_ratio=0.8
    )

    assert bitrate == 1200
