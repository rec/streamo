from pathlib import Path

import pytest

from scripts import preview_loops


def test_playback_preview_command_uses_four_second_loop_boundary_window() -> None:
    command = preview_loops.playback_preview_command(
        Path('movie-looped.mp4'), Path('preview.mp4'), duration=12.25
    )

    filters = command[command.index('-filter_complex') + 1]
    assert 'trim=start=10.250:end=12.250' in filters
    assert 'trim=start=0.000:end=2.000' in filters
    assert '[tail][head]concat=n=2:v=1:a=0[out]' in filters
    assert command[command.index('-map') + 1] == '[out]'
    assert command[-1] == 'preview.mp4'


def test_playback_preview_command_starts_at_zero_for_short_videos() -> None:
    command = preview_loops.playback_preview_command(
        Path('movie-looped.mp4'), Path('preview.mp4'), duration=1.5
    )

    filters = command[command.index('-filter_complex') + 1]
    assert filters.count('trim=start=0.000:end=1.500') == 2


def test_preview_command_plays_finite_file_without_looping() -> None:
    command = preview_loops.preview_command(Path('preview.mp4'))

    assert command[:5] == [
        'ffplay',
        '-hide_banner',
        '-loglevel',
        'warning',
        '-autoexit',
    ]
    assert '-loop' not in command
    assert '-stream_loop' not in command
    assert command[-1] == 'preview.mp4'


def test_preview_loop_moves_looped_files_to_loops(
    monkeypatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    video = tmp_path / 'movie-looped.mp4'
    video.write_text('looped')
    calls: list[str] = []

    def write_loop(video: Path, preview: Path) -> None:
        calls.append('write')

    monkeypatch.setattr(preview_loops, 'write_loop', write_loop)

    preview_loops.preview_loop(video)

    assert calls == []
    assert 'Moving existing loop' in capsys.readouterr().out
    assert not video.exists()
    assert (tmp_path / 'loops' / 'movie-looped.mp4').read_text() == 'looped'


def test_move_to_loops_keeps_files_already_in_loops(tmp_path: Path) -> None:
    loops = tmp_path / 'loops'
    loops.mkdir()
    video = loops / 'movie-looped.mp4'
    video.write_text('looped')

    assert preview_loops.move_to_loops(video) == video
    assert video.read_text() == 'looped'


def test_accept_loop_moves_preview_and_original(tmp_path: Path) -> None:
    video = tmp_path / 'movie.mp4'
    preview = tmp_path / 'preview.mp4'
    video.write_text('original')
    preview.write_text('looped')

    preview_loops.accept_loop(video, preview)

    assert (tmp_path / 'loops' / 'movie-looped.mp4').read_text() == 'looped'
    assert (tmp_path / 'originals' / 'movie.mp4').read_text() == 'original'
    assert not video.exists()
    assert not preview.exists()


def test_preview_loop_prints_controls_before_preview(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('original')
    calls: list[str] = []

    def write_loop(video: Path, preview: Path) -> None:
        calls.append('write-loop')

    def play_preview(preview: Path) -> None:
        calls.append('play')

    monkeypatch.setattr(preview_loops, 'write_loop', write_loop)

    def write_playback_preview(source: Path, playback: Path) -> None:
        calls.append(source.name)
        playback.touch()

    monkeypatch.setattr(preview_loops, 'write_playback_preview', write_playback_preview)
    monkeypatch.setattr(preview_loops, 'play_preview', play_preview)
    monkeypatch.setattr('builtins.input', lambda prompt: '')

    preview_loops.preview_loop(video)

    output = capsys.readouterr().out
    assert 'Preparing playback preview' in output
    assert 'Playing preview' in output
    assert 'r=replay, l=loop, m=mark as looping, return=skip' in output
    assert calls == ['movie.mp4', 'play']
    assert video.read_text() == 'original'
    assert not (tmp_path / 'loops').exists()


def test_preview_loop_marks_existing_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('original')
    calls: list[str] = []

    def write_loop(video: Path, preview: Path) -> None:
        calls.append('write-loop')

    monkeypatch.setattr(preview_loops, 'write_loop', write_loop)
    monkeypatch.setattr(
        preview_loops, 'write_playback_preview', lambda video, output: None
    )
    monkeypatch.setattr(preview_loops, 'play_preview', lambda preview: None)
    monkeypatch.setattr('builtins.input', lambda prompt: 'm')

    preview_loops.preview_loop(video)

    assert calls == []
    assert not video.exists()
    assert (tmp_path / 'loops' / 'movie.mp4').read_text() == 'original'
    assert not (tmp_path / 'loops' / 'movie-looped.mp4').exists()


def test_preview_loop_replays_before_accepting(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('original')
    answers = iter(['r', 'l'])
    calls: list[str] = []

    def write_loop(video: Path, preview: Path) -> None:
        preview.touch()

    def play_preview(preview: Path) -> None:
        calls.append('play')

    monkeypatch.setattr(preview_loops, 'write_loop', write_loop)
    monkeypatch.setattr(preview_loops, 'write_playback_preview', write_loop)
    monkeypatch.setattr(preview_loops, 'play_preview', play_preview)
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))

    preview_loops.preview_loop(video)

    assert calls == ['play', 'play']
    assert (tmp_path / 'loops' / 'movie-looped.mp4').exists()
    assert (tmp_path / 'originals' / 'movie.mp4').read_text() == 'original'


def test_preview_loop_accept_generates_loop_after_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('original')
    calls: list[str] = []

    def write_loop(video: Path, preview: Path) -> None:
        calls.append('write-loop')
        preview.write_text('looped')

    monkeypatch.setattr(preview_loops, 'write_loop', write_loop)
    monkeypatch.setattr(
        preview_loops, 'write_playback_preview', lambda video, output: None
    )
    monkeypatch.setattr(preview_loops, 'play_preview', lambda preview: None)
    monkeypatch.setattr('builtins.input', lambda prompt: 'l')

    preview_loops.preview_loop(video)

    assert calls == ['write-loop']
    assert (tmp_path / 'loops' / 'movie-looped.mp4').read_text() == 'looped'
