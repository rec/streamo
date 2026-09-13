import subprocess as sp
from pathlib import Path

import numpy as np
import pytest

from scripts import auto_tester


def test_mean_difference_detects_visually_different_frames() -> None:
    first = np.zeros((64, 64, 3), dtype=np.uint8)
    last = np.full((64, 64, 3), 255, dtype=np.uint8)

    assert auto_tester.mean_difference(first, last) == 255.0


def test_mean_difference_treats_identical_frames_as_possible_loop() -> None:
    frame = np.full((64, 64, 3), 64, dtype=np.uint8)

    assert auto_tester.mean_difference(frame, frame) == 0.0


def test_auto_test_loops_definite_non_loop(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('original')

    def run_silent(command: list[str], *, text: bool = False) -> sp.CompletedProcess:
        Path(command[-1]).write_text('looped')
        return sp.CompletedProcess(command, 0)

    monkeypatch.setattr(auto_tester, 'endpoint_difference', lambda video: 255.0)
    monkeypatch.setattr(auto_tester.loop_videos, 'count_frames', lambda video: 10)
    monkeypatch.setattr(auto_tester, 'run_silent', run_silent)

    auto_tester.auto_test(video)

    output = capsys.readouterr().out
    assert 'Comparing loop endpoints' in output
    assert 'Looping definitely non-loop file' in output
    assert 'Counting frames' in output
    assert 'Writing loop' in output
    assert 'Moving original' in output
    assert (tmp_path / 'loops' / 'movie-looped.mp4').read_text() == 'looped'
    assert (tmp_path / 'originals' / 'movie.mp4').read_text() == 'original'
    assert not video.exists()


def test_auto_test_leaves_possible_loop_in_place(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('possible loop')

    monkeypatch.setattr(auto_tester, 'endpoint_difference', lambda video: 0.0)

    auto_tester.auto_test(video)

    output = capsys.readouterr().out
    assert 'Comparing loop endpoints' in output
    assert 'Leaving possible loop in place' in output
    assert video.read_text() == 'possible loop'
    assert not (tmp_path / 'loops').exists()
    assert not (tmp_path / 'originals').exists()


def test_auto_test_uses_configured_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie.mp4'
    video.write_text('candidate')

    monkeypatch.setattr(auto_tester, 'endpoint_difference', lambda video: 12.0)

    def write_loop(video: Path, output: Path) -> None:
        output.parent.mkdir()
        output.touch()

    monkeypatch.setattr(auto_tester, 'write_loop', write_loop)
    monkeypatch.setattr(auto_tester, 'move_original', lambda video: video)

    auto_tester.auto_test(video, threshold=10.0)

    assert (tmp_path / 'loops' / 'movie-looped.mp4').exists()


def test_auto_test_default_threshold_accepts_ten() -> None:
    assert auto_tester.DEFAULT_THRESHOLD == 10.0


def test_auto_test_leaves_looped_names_in_place(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / 'movie-looped.mp4'
    video.write_text('loop')
    calls: list[Path] = []

    def endpoint_difference(video: Path) -> float:
        calls.append(video)
        return 255.0

    monkeypatch.setattr(auto_tester, 'endpoint_difference', endpoint_difference)

    auto_tester.auto_test(video)

    assert calls == []
    assert video.read_text() == 'loop'
    assert not (tmp_path / 'loops').exists()
