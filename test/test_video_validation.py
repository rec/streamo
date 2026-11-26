import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from reccy.configuration.settings import write_text_atomically

from streamo import daemon
from streamo.config import Streamo


@pytest.fixture(scope='module')
def movies(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and ffprobe are required for movie validation tests')
    directory = tmp_path_factory.mktemp('movie-cadence')
    paths = {}
    for mode in ('constant', 'variable'):
        path = directory / f'{mode}.mkv'
        timing = (
            ['-vf', r'settb=1/30000,setpts=PTS+mod(N\,2)*300']
            if mode == 'variable'
            else []
        )
        subprocess.run(
            [
                'ffmpeg',
                '-v',
                'error',
                '-f',
                'lavfi',
                '-i',
                'testsrc2=size=64x48:rate=30:duration=1',
                *timing,
                '-c:v',
                'mpeg4',
                '-threads',
                '1',
                '-enc_time_base',
                '1:30000',
                '-fps_mode',
                'passthrough',
                str(path),
            ],
            check=True,
            timeout=10,
        )
        paths[mode] = path
    return paths


def configuration(
    tmp_path: Path,
    movie: Path,
    settings: str = '',
    resolution: str = '64x48',
    frame_rate: int = 30,
) -> Path:
    path = tmp_path / 'streamo.toml'
    write_text_atomically(
        path,
        f"video = '{movie.as_posix()}'\n{settings}"
        '\n[streaming_service.ingest]\nstream_key = "unused"\n'
        f'\n[streaming_service.encoding.video]\nresolution = "{resolution}"\n'
        f'frame_rate = {frame_rate}\n',
    )
    return path


def test_validate_accepts_constant_cadence_rounded_to_container_ticks(
    tmp_path: Path, movies: dict[str, Path]
) -> None:
    config = configuration(tmp_path, movies['constant'])
    assert daemon.run(daemon.DaemonOptions(action='validate', config=config)) == 0


def test_provisioning_rejects_irregular_cadence_at_the_expected_average_rate(
    tmp_path: Path, movies: dict[str, Path]
) -> None:
    config = configuration(tmp_path, movies['variable'])
    daemon.load_config(config).validate_media()
    with pytest.raises(
        SystemExit, match='64x48 at 30 fps.*constant 30 fps.*irregular timing'
    ):
        daemon.run(daemon.DaemonOptions(action='validate', config=config))


def test_explicit_scaling_allows_a_movie_that_requires_frame_rate_conversion(
    tmp_path: Path, movies: dict[str, Path]
) -> None:
    config = configuration(tmp_path, movies['variable'], 'allow_scaling = true\n')
    assert daemon.run(daemon.DaemonOptions(action='validate', config=config)) == 0


@pytest.mark.parametrize('action', ['validate', 'run', 'preview'])
def test_wrong_video_format_fails_before_starting_any_service(
    tmp_path: Path, movies: dict[str, Path], action: str
) -> None:
    config = configuration(tmp_path, movies['constant'], resolution='1280x720')
    with (
        mock.patch.object(Streamo, 'start') as start,
        pytest.raises(SystemExit, match='64x48 at 30 fps.*Expected 1280x720 at 30 fps'),
    ):
        daemon.run(
            daemon.DaemonOptions.model_validate({'action': action, 'config': config})
        )
    start.assert_not_called()


def test_validate_rejects_a_movie_with_matching_size_and_wrong_frame_rate(
    tmp_path: Path, movies: dict[str, Path]
) -> None:
    config = configuration(tmp_path, movies['constant'], frame_rate=24)
    with pytest.raises(SystemExit, match='64x48 at 30 fps.*Expected 64x48 at 24 fps'):
        daemon.run(daemon.DaemonOptions(action='validate', config=config))
