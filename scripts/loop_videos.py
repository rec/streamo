#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from reccy.runtime.process import run_silent


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Create seamless forward/backward loops from video files.'
    )
    parser.add_argument('videos', nargs='+', type=Path)
    args = parser.parse_args()

    for video in args.videos:
        loop_video(video)


def loop_video(video: Path) -> Path:
    if not video.exists():
        sys.exit(f'{video} does not exist')

    frame_count = count_frames(video)
    if frame_count < 3:
        sys.exit(f'{video} has fewer than 3 frames')

    output = looped_path(video)
    run_silent(ffmpeg_command(video, output, frame_count))
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
    return int(result.stdout.strip())


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
        '-y',
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
