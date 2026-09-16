from pathlib import Path

import pytest

from scripts.media_output import new_media_output
from scripts.reencode_videos import reencode_files


def test_failed_render_leaves_no_partial_output(tmp_path: Path) -> None:
    output = tmp_path / 'output.mp4'
    with pytest.raises(RuntimeError), new_media_output(output) as temporary:
        temporary.write_bytes(b'partial')
        raise RuntimeError('render failed')
    assert not output.exists()


def test_completed_render_never_replaces_a_concurrent_output(tmp_path: Path) -> None:
    output = tmp_path / 'output.mp4'
    with pytest.raises(FileExistsError), new_media_output(output) as temporary:
        temporary.write_bytes(b'new render')
        output.write_bytes(b'existing render')
    assert output.read_bytes() == b'existing render'


def test_reencode_rejects_duplicate_stems_before_rendering(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match='duplicate output'):
        reencode_files([Path('clip.mov'), Path('clip.mp4')], tmp_path)


def test_existing_output_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / 'clip.mp4'
    output.write_bytes(b'existing render')
    with pytest.raises(SystemExit, match='already exists'):
        reencode_files([Path('clip.mov')], tmp_path)
    assert output.read_bytes() == b'existing render'
