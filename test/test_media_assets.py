from pathlib import Path

import pytest

from streamo.media_assets import prepare_video


def test_video_path_remains_a_snapshot_until_consumer_exits(tmp_path: Path) -> None:
    source = tmp_path / 'movie.mp4'
    source.write_bytes(b'original video')
    with prepare_video(source) as path:
        source.write_bytes(b'replacement video')
        assert path.read_bytes() == b'original video'
    assert not path.exists()
    assert source.read_bytes() == b'replacement video'


def test_video_snapshot_is_cleaned_up_when_consumer_fails(tmp_path: Path) -> None:
    source = tmp_path / 'movie.mp4'
    source.write_bytes(b'video')
    with pytest.raises(ValueError, match='encoder failed'):
        with prepare_video(source) as path:
            raise ValueError('encoder failed')
    assert not path.exists()
