#!/usr/bin/env python3
import sys
from pathlib import Path
from typing import Annotated, cast

import tyro
from pydantic import BaseModel
from reccy.runtime.process import run_silent

from .media_output import new_media_output

DEFAULT_MAX_BITRATE_KBPS = 1200
DEFAULT_SOURCE_RATIO = 0.8


class ReencodeVideos(BaseModel, frozen=True):
    """Write 720p H.264 mezzanine files, rejecting existing or duplicate outputs."""

    output_directory: Annotated[Path, tyro.conf.Positional]
    videos: Annotated[list[Path], tyro.conf.Positional]
    max_bitrate_kbps: int = DEFAULT_MAX_BITRATE_KBPS
    source_ratio: float = DEFAULT_SOURCE_RATIO


def main() -> None:
    options = tyro.cli(ReencodeVideos)
    reencode_files(
        options.videos,
        options.output_directory,
        max_bitrate_kbps=options.max_bitrate_kbps,
        source_ratio=options.source_ratio,
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
    targets = [output_directory / f'{v.stem}.mp4' for v in videos]
    if len(set(targets)) != len(targets):
        sys.exit('input filenames produce duplicate output paths')
    for target in targets:
        if target.exists():
            sys.exit(f'{target} already exists')
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
    with new_media_output(output) as temporary:
        run_silent(reencode_command(video, temporary, bitrate_kbps=bitrate_kbps))


def reencode_command(video: Path, output: Path, *, bitrate_kbps: int) -> list[str]:
    return [
        'ffmpeg',
        '-hide_banner',
        '-n',
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
    return float(cast(str, result.stdout).strip())


if __name__ == '__main__':
    main()
