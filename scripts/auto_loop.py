#!/usr/bin/env python3
import shutil
import sys
from pathlib import Path
from typing import Annotated, cast

import numpy as np
import tyro
from pydantic import BaseModel
from reccy.runtime.process import run_silent

from . import loop_videos
from .media_output import new_media_output

FRAME_SIZE = 64
FRAME_CHANNELS = 3
FRAME_BYTE_COUNT = FRAME_SIZE * FRAME_SIZE * FRAME_CHANNELS
DEFAULT_THRESHOLD = 10.0


class AutoLoop(BaseModel, frozen=True):
    """Render loops when sampled endpoints differ, then move sources to originals/."""

    videos: Annotated[list[Path], tyro.conf.Positional]
    threshold: float = DEFAULT_THRESHOLD


def main() -> None:
    options = tyro.cli(AutoLoop)
    for video in options.videos:
        auto_loop(video, threshold=options.threshold)


def auto_loop(video: Path, *, threshold: float = DEFAULT_THRESHOLD) -> None:
    if is_named_loop(video):
        print(f'Leaving possible loop in place: {video}')
        return
    if not video.exists():
        sys.exit(f'{video} does not exist')

    print(f'Comparing loop endpoints for {video}...')
    difference = endpoint_difference(video)
    if difference < threshold:
        print(f'Leaving possible loop in place: {video} ({difference:.1f})')
        return

    for target in (looped_output(video), video.parent / 'originals' / video.name):
        if target.exists():
            sys.exit(f'{target} already exists')
    print(f'Looping file with differing sampled endpoints: {video} ({difference:.1f})')
    write_loop(video, looped_output(video))
    move_original(video)


def is_named_loop(video: Path) -> bool:
    return 'looped' in video.name.lower()


def endpoint_difference(video: Path) -> float:
    first = frame_sample(first_frame_command(video))
    last = frame_sample(last_frame_command(video))
    return mean_difference(first, last)


def first_frame_command(video: Path) -> list[str]:
    return frame_command(video, seek_from_end=False)


def last_frame_command(video: Path) -> list[str]:
    return frame_command(video, seek_from_end=True)


def frame_command(video: Path, *, seek_from_end: bool) -> list[str]:
    command = ['ffmpeg', '-hide_banner', '-v', 'error']
    if seek_from_end:
        command.extend(['-sseof', '-0.25'])
    command.extend(
        [
            '-i',
            video.as_posix(),
            '-frames:v',
            '1',
            '-vf',
            f'scale={FRAME_SIZE}:{FRAME_SIZE},format=rgb24',
            '-f',
            'rawvideo',
            '-',
        ]
    )
    return command


def frame_sample(command: list[str]) -> np.ndarray:
    data = cast(bytes, run_silent(command).stdout)
    if len(data) != FRAME_BYTE_COUNT:
        sys.exit(f'Expected {FRAME_BYTE_COUNT} frame bytes, got {len(data)}')
    return np.frombuffer(data, dtype=np.uint8).reshape(
        (FRAME_SIZE, FRAME_SIZE, FRAME_CHANNELS)
    )


def mean_difference(first: np.ndarray, last: np.ndarray) -> float:
    return float(np.abs(first.astype(np.int16) - last.astype(np.int16)).mean())


def looped_output(video: Path) -> Path:
    return video.parent / 'loops' / loop_videos.looped_path(video).name


def write_loop(video: Path, output: Path) -> None:
    output.parent.mkdir(exist_ok=True)
    print(f'Counting frames in {video}...')
    frame_count = loop_videos.count_frames(video)
    if frame_count < 3:
        sys.exit(f'{video} has fewer than 3 frames')
    print(f'Writing loop to {output}...')
    with new_media_output(output) as temporary:
        run_silent(loop_videos.ffmpeg_command(video, temporary, frame_count))


def move_original(video: Path) -> Path:
    originals = video.parent / 'originals'
    originals.mkdir(exist_ok=True)
    target = originals / video.name
    if target.exists():
        sys.exit(f'{target} already exists')
    print(f'Moving original to {target}...')
    shutil.move(video.as_posix(), target.as_posix())
    return target


if __name__ == '__main__':
    main()
