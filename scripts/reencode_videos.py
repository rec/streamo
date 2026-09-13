#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from reccy.runtime.process import run_silent

DEFAULT_MAX_BITRATE_KBPS = 1200
DEFAULT_SOURCE_RATIO = 0.8


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Re-encode videos as 720p H.264 mezzanine files.'
    )
    parser.add_argument(
        '--max-bitrate-kbps',
        type=int,
        default=DEFAULT_MAX_BITRATE_KBPS,
        help='Maximum video bitrate for re-encoded files.',
    )
    parser.add_argument(
        '--source-ratio',
        type=float,
        default=DEFAULT_SOURCE_RATIO,
        help='Target this fraction of the source file bitrate.',
    )
    parser.add_argument('output_directory', type=Path)
    parser.add_argument('videos', nargs='+', type=Path)
    args = parser.parse_args()

    reencode_files(
        args.videos,
        args.output_directory,
        max_bitrate_kbps=args.max_bitrate_kbps,
        source_ratio=args.source_ratio,
    )


def reencode_files(
    videos: list[Path],
    output_directory: Path,
    *,
    max_bitrate_kbps: int = DEFAULT_MAX_BITRATE_KBPS,
    source_ratio: float = DEFAULT_SOURCE_RATIO,
) -> None:
    if output_directory.exists() and not output_directory.is_dir():
        sys.exit(f'{output_directory} is not a directory')

    output_directory.mkdir(exist_ok=True)
    for video in videos:
        if not video.is_file():
            sys.exit(f'{video} is not a file')
        reencode_video(
            video,
            output_directory / f'{video.stem}.mp4',
            max_bitrate_kbps=max_bitrate_kbps,
            source_ratio=source_ratio,
        )


def reencode_video(
    video: Path,
    output: Path,
    *,
    max_bitrate_kbps: int = DEFAULT_MAX_BITRATE_KBPS,
    source_ratio: float = DEFAULT_SOURCE_RATIO,
) -> None:
    bitrate_kbps = target_bitrate_kbps(
        video, max_bitrate_kbps=max_bitrate_kbps, source_ratio=source_ratio
    )
    print(f'Re-encoding {video} to {output} at {bitrate_kbps} kbps...')
    run_silent(reencode_command(video, output, bitrate_kbps=bitrate_kbps))


def reencode_command(video: Path, output: Path, *, bitrate_kbps: int) -> list[str]:
    return [
        'ffmpeg',
        '-hide_banner',
        '-y',
        '-i',
        video.as_posix(),
        '-vf',
        'scale=1280:-2,fps=30',
        '-an',
        '-c:v',
        'libx264',
        '-b:v',
        f'{bitrate_kbps}k',
        '-maxrate',
        f'{bitrate_kbps}k',
        '-bufsize',
        f'{bitrate_kbps * 2}k',
        '-preset',
        'slow',
        '-pix_fmt',
        'yuv420p',
        output.as_posix(),
    ]


def target_bitrate_kbps(
    video: Path,
    *,
    max_bitrate_kbps: int = DEFAULT_MAX_BITRATE_KBPS,
    source_ratio: float = DEFAULT_SOURCE_RATIO,
) -> int:
    source_kbps = video.stat().st_size * 8 / duration(video) / 1000
    return max(1, min(max_bitrate_kbps, int(source_kbps * source_ratio)))


def duration(video: Path) -> float:
    result = run_silent(
        [
            'ffprobe',
            '-v',
            'error',
            '-show_entries',
            'format=duration',
            '-of',
            'default=nokey=1:noprint_wrappers=1',
            video.as_posix(),
        ],
        text=True,
    )
    return float(result.stdout.strip())


if __name__ == '__main__':
    main()
