import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from scripts import loop_videos, preview_loops, reencode_videos, render


@pytest.mark.parametrize(
    ('module', 'probe', 'output'),
    [
        (loop_videos, loop_videos.count_frames, 'N/A'),
        (preview_loops, preview_loops.duration, 'nan'),
        (reencode_videos, reencode_videos.duration, '0'),
        (render, render.probe_duration, 'inf'),
    ],
)
def test_invalid_probe_result_names_the_file(
    module: ModuleType,
    probe: Callable[[Path], float | int],
    output: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        'run_silent',
        lambda command, text=True: subprocess.CompletedProcess(
            command, 0, stdout=output
        ),
    )
    path = Path('broken.mp4')
    with pytest.raises(SystemExit, match='broken.mp4'):
        probe(path)
