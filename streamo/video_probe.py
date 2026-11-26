"""Inspect the movie stream selected by FFmpeg."""

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field

from .provider_config import VideoEncoding


class VideoProbeError(ValueError):
    pass


class DisplayMatrix(BaseModel, frozen=True):
    rotation: int = 0


class VideoStream(BaseModel, frozen=True):
    codec_name: str | None = None
    width: int
    height: int
    avg_frame_rate: str
    time_base: str
    side_data_list: list[DisplayMatrix] = Field(default_factory=list)

    @property
    def resolution(self) -> str:
        if any(m.rotation % 180 == 90 for m in self.side_data_list):
            return f'{self.height}x{self.width}'
        return f'{self.width}x{self.height}'

    @property
    def format_description(self) -> str:
        return f'{self.resolution} at {self.avg_frame_rate.removesuffix("/1")} fps'

    def validate_format(self, movie: Path, expected: VideoEncoding) -> None:
        try:
            matches_rate = Fraction(self.avg_frame_rate) == expected.frame_rate
        except (ValueError, ZeroDivisionError):
            matches_rate = False
        if not matches_rate or self.resolution != expected.resolution:
            raise VideoProbeError(
                f'Movie {movie} is {self.format_description}. '
                f'Expected {expected.resolution} at {expected.frame_rate} fps.'
            )


class VideoProbe(BaseModel, frozen=True):
    streams: list[VideoStream] = Field(default_factory=list)
    format: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def read(cls, movie: Path) -> Self:
        if not movie.is_file():
            raise VideoProbeError(f'Video file is unavailable: {movie}')
        try:
            result = subprocess.run(
                [
                    'ffprobe',
                    '-v',
                    'error',
                    '-select_streams',
                    'v:0',
                    '-show_entries',
                    'stream=codec_name,width,height,avg_frame_rate,time_base:'
                    'stream_side_data=rotation:format=duration',
                    '-of',
                    'json',
                    str(movie),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return cls.model_validate_json(result.stdout)
        except FileNotFoundError as error:
            raise VideoProbeError(
                'ffprobe is required to validate the movie'
            ) from error
        except subprocess.CalledProcessError as error:
            raise VideoProbeError(
                f'Could not probe movie {movie}: {error.stderr.strip()}'
            ) from error
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise VideoProbeError(
                f'Could not probe movie {movie} ({type(error).__name__})'
            ) from error

    def video(self) -> VideoStream:
        if not self.streams:
            raise VideoProbeError('Visual bed has no video stream')
        return self.streams[0]


def validate_cadence(movie: Path, video: VideoStream, expected: VideoEncoding) -> None:
    """Check every decoded frame in presentation order, allowing tick rounding."""
    description = (
        f'Movie {movie} reports {video.format_description}. Expected '
        f'{expected.resolution} at a constant {expected.frame_rate} fps.'
    )
    try:
        step = 1 / (Fraction(video.time_base) * expected.frame_rate)
    except (ValueError, ZeroDivisionError) as error:
        raise VideoProbeError(f'{description} Invalid video time base.') from error
    if step < 1:
        raise VideoProbeError(f'{description} Video time base is too coarse.')
    step_ticks, scale = step.numerator, step.denominator
    min_delta = step_ticks // scale
    max_delta = -(-step_ticks // scale)
    first = previous = None
    frame_count = 0
    probe_error = ''
    try:
        with subprocess.Popen(
            [
                'ffprobe',
                '-v',
                'error',
                '-select_streams',
                'v:0',
                '-show_frames',
                '-show_entries',
                'frame=best_effort_timestamp,width,height',
                '-of',
                'compact',
                str(movie),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    if not line.startswith('frame|'):
                        probe_error = probe_error or line.strip()
                        continue
                    frame_count += 1
                    fields = {
                        k: v
                        for k, s, v in (
                            f.partition('=') for f in line.rstrip().split('|')[1:]
                        )
                        if s
                    }
                    try:
                        timestamp = int(fields['best_effort_timestamp'])
                        width, height = int(fields['width']), int(fields['height'])
                    except (KeyError, ValueError) as error:
                        raise VideoProbeError(
                            f'{description} Frame {frame_count} has no usable '
                            'timing or size.'
                        ) from error
                    if (width, height) != (video.width, video.height):
                        raise VideoProbeError(
                            f'{description} Frame {frame_count} is {width}x{height}.'
                        )
                    if first is None:
                        first = timestamp
                    if previous is not None and (
                        not min_delta <= timestamp - previous <= max_delta
                        or abs(
                            (timestamp - first) * scale - (frame_count - 1) * step_ticks
                        )
                        > scale
                    ):
                        delta = Fraction(timestamp - previous) * Fraction(
                            video.time_base
                        )
                        raise VideoProbeError(
                            f'{description} Frame {frame_count} has irregular timing '
                            f'({float(delta) * 1000:.3f} ms since the previous frame).'
                        )
                    previous = timestamp
                if process.wait() or probe_error or not frame_count:
                    raise VideoProbeError(
                        f'{description} Could not verify all frames: '
                        f'{probe_error or "no complete decoded video stream"}.'
                    )
            finally:
                if process.poll() is None:
                    process.kill()
    except OSError as error:
        raise VideoProbeError(
            f'{description} Could not run ffprobe: {error}'
        ) from error
