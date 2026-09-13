#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from reccy.runtime.process import run_silent

from scripts import loop_videos

FRAME_SIZE = 64
FRAME_CHANNELS = 3
FRAME_BYTE_COUNT = FRAME_SIZE * FRAME_SIZE * FRAME_CHANNELS
DEFAULT_THRESHOLD = 10.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Automatically loop videos that are clearly not seamless loops.'
    )
    parser.add_argument(
        '--threshold',
        type=float,
        default=DEFAULT_THRESHOLD,
        help='Mean endpoint-frame difference needed to auto-loop a file.',
    )
    parser.add_argument('videos', nargs='+', type=Path)
    args = parser.parse_args()

    for video in args.videos:
        auto_test(video, threshold=args.threshold)


def auto_test(video: Path, *, threshold: float = DEFAULT_THRESHOLD) -> None:
    if ignored(video):
        print(f'Leaving possible loop in place: {video}')
        return
    if not video.exists():
        sys.exit(f'{video} does not exist')

    print(f'Comparing loop endpoints for {video}...')
    difference = endpoint_difference(video)
    if difference < threshold:
        print(f'Leaving possible loop in place: {video} ({difference:.1f})')
        return

    print(f'Looping definitely non-loop file: {video} ({difference:.1f})')
    write_loop(video, looped_output(video))
    move_original(video)


def ignored(video: Path) -> bool:
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
    data = run_silent(command).stdout
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
    run_silent(loop_videos.ffmpeg_command(video, output, frame_count))


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
