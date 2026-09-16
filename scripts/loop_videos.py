#!/usr/bin/env python3
import sys
from pathlib import Path
from typing import Annotated, cast

import tyro
from pydantic import BaseModel
from reccy.runtime.process import run_silent

from .media_output import new_media_output


class LoopVideos(BaseModel, frozen=True):
    """Create forward/backward loops beside each source without replacing files."""

    videos: Annotated[list[Path], tyro.conf.Positional]


def main() -> None:
    options = tyro.cli(LoopVideos)
    for video in options.videos:
        loop_video(video)


def loop_video(video: Path) -> Path:
    if not video.exists():
        sys.exit(f'{video} does not exist')

    frame_count = count_frames(video)
    if frame_count < 3:
        sys.exit(f'{video} has fewer than 3 frames')

    output = looped_path(video)
    with new_media_output(output) as temporary:
        run_silent(ffmpeg_command(video, temporary, frame_count))
    return output


def looped_path(video: Path) -> Path:
    return video.with_name(f'{video.stem}-looped{video.suffix}')


def count_frames(video: Path) -> int:
    result = run_silent(
        [
            'ffprobe',
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-count_frames',
            '-show_entries',
            'stream=nb_read_frames',
            '-of',
            'default=nokey=1:noprint_wrappers=1',
            video.as_posix(),
        ],
        text=True,
    )
    return int(cast(str, result.stdout).strip())


def ffmpeg_command(video: Path, output: Path, frame_count: int) -> list[str]:
    last_reverse_frame = frame_count - 1
    filters = (
        '[0:v]split=2[fwd][revsrc];'
        '[fwd]setpts=PTS-STARTPTS[fwd];'
        f'[revsrc]reverse,trim=start_frame=1:end_frame={last_reverse_frame},'
        'setpts=PTS-STARTPTS[rev];'
        '[fwd][rev]concat=n=2:v=1:a=0[out]'
    )
    return [
        'ffmpeg',
        '-hide_banner',
        '-n',
        '-i',
        video.as_posix(),
        '-filter_complex',
        filters,
        '-map',
        '[out]',
        '-an',
        '-c:v',
        'libx264',
        '-pix_fmt',
        'yuv420p',
        '-movflags',
        '+faststart',
        output.as_posix(),
    ]


if __name__ == '__main__':
    main()
