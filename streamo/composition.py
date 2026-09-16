from dataclasses import dataclass
from pathlib import Path

from .config import Streamo
from .providers import FfmpegDestination, FfmpegOutput, ingest_output

LOCAL_DISPLAY_URL = 'udp://127.0.0.1:23000?pkt_size=1316'


@dataclass(frozen=True)
class VideoOverlay:
    name: str
    image: Path
    input_index: int
    gap_index: int
    interval: float
    duration: float
    fade: float


def ffmpeg_command(
    config: Streamo,
    *,
    output: FfmpegOutput | None = None,
    image_pipe: int | None = None,
    preview: bool = False,
) -> list[str]:
    encoding = config.streaming_service.encoding
    overlays: list[VideoOverlay] = []
    image_input: int | None = None
    next_input = 2
    command = [
        'ffmpeg',
        '-hide_banner',
        '-loglevel',
        'warning',
        '-nostats',
        '-progress',
        'pipe:2',
        '-f',
        'f32le',
        '-ar',
        str(config.sample_rate),
        '-ac',
        '2',
        '-i',
        'pipe:0',
    ]
    if encoding.video is not None:
        assert config.video is not None
        command.extend(
            [
                '-re',
                '-stream_loop',
                '-1',
                '-i',
                config.video.as_posix(),
            ]
        )
    if encoding.video is not None and config.title_card is not None:
        overlays.append(
            VideoOverlay(
                name='title',
                image=config.title_card,
                input_index=next_input,
                gap_index=next_input + 1,
                interval=config.title_interval,
                duration=config.title_duration,
                fade=config.title_fade,
            )
        )
        command.extend(overlay_input_args(config, overlays[-1]))
        next_input += 2
    if encoding.video is not None and config.image_interval > 0:
        if image_pipe is None:
            raise ValueError(
                'image pipe is required when participant images are enabled'
            )
        image_input = next_input
        width, height = video_size(config)
        command.extend(
            [
                '-f',
                'rawvideo',
                '-pixel_format',
                'rgba',
                '-video_size',
                f'{width}x{height}',
                '-framerate',
                str(config.video_frame_rate),
                '-i',
                f'pipe:{image_pipe}',
            ]
        )
    if overlays or image_input is not None:
        command.extend(
            [
                '-filter_complex',
                overlay_filter(config, overlays, image_input=image_input),
                '-map',
                '[video]',
            ]
        )
    elif encoding.video is not None:
        command.extend(['-map', '1:v:0'])
    command.extend(['-map', '0:a:0'])
    if encoding.video is not None:
        command.extend(
            [
                '-c:v',
                VIDEO_CODECS[encoding.video.codec],
                '-b:v',
                encoding.video.bitrate,
                '-pix_fmt',
                encoding.video.pixel_format,
                '-r',
                str(encoding.video.frame_rate),
                '-s',
                encoding.video.resolution,
                '-g',
                str(
                    round(encoding.video.frame_rate * encoding.video.keyframe_interval)
                ),
            ]
        )
        if encoding.video.codec in {'h264', 'hevc'}:
            command.extend(['-preset', 'veryfast'])
        if encoding.video.codec == 'h264':
            command.extend(['-tune', 'animation'])
    command.extend(
        [
            '-c:a',
            AUDIO_CODECS[encoding.audio.codec],
            '-b:a',
            encoding.audio.bitrate,
            '-ar',
            str(encoding.audio.sample_rate),
            '-ac',
            str(encoding.audio.channels),
        ]
    )
    if preview:
        command.extend(['-f', 'nut', 'pipe:1'])
    else:
        command.extend(
            (
                output
                if output is not None
                else local_display_output(
                    config, ingest_output(config.streaming_service)
                )
            ).arguments
        )
    return command


def local_display_output(config: Streamo, output: FfmpegOutput | None) -> FfmpegOutput:
    output = output or ingest_output(config.streaming_service)
    if not config.local_display or config.streaming_service.encoding.video is None:
        return output
    return output.model_copy(
        update={
            'destinations': [
                *output.destinations,
                FfmpegDestination(
                    muxer='mpegts', url=LOCAL_DISPLAY_URL, secret_url=False
                ),
            ]
        }
    )


def title_input_args(config: Streamo) -> list[str]:
    assert config.title_card is not None
    return overlay_input_args(
        config,
        VideoOverlay(
            name='title',
            image=config.title_card,
            input_index=2,
            gap_index=3,
            interval=config.title_interval,
            duration=config.title_duration,
            fade=config.title_fade,
        ),
    )


def overlay_input_args(config: Streamo, overlay: VideoOverlay) -> list[str]:
    gap_duration = overlay.interval - overlay.duration
    width, height = video_size(config)
    return [
        '-loop',
        '1',
        '-t',
        f'{overlay.duration:.6f}',
        '-i',
        overlay.image.as_posix(),
        '-f',
        'lavfi',
        '-t',
        f'{gap_duration:.6f}',
        '-i',
        (
            'color='
            f'c=black@0.0:s={width}x{height}:'
            f'r={config.video_frame_rate}:d={gap_duration:.6f}'
        ),
    ]


def title_filter(config: Streamo) -> str:
    assert config.title_card is not None
    return overlay_filter(
        config,
        [
            VideoOverlay(
                name='title',
                image=config.title_card,
                input_index=2,
                gap_index=3,
                interval=config.title_interval,
                duration=config.title_duration,
                fade=config.title_fade,
            )
        ],
    )


def overlay_filter(
    config: Streamo,
    overlays: list[VideoOverlay],
    *,
    image_input: int | None = None,
) -> str:
    encoding = config.streaming_service.encoding.video
    assert encoding is not None
    width, height = encoding.resolution.lower().split('x')
    parts = [
        f'[1:v]scale={width}:{height},fps={encoding.frame_rate},format=yuv420p[base];'
    ]
    current = 'base'
    for index, overlay in enumerate(overlays):
        output = (
            'video'
            if index == len(overlays) - 1 and image_input is None
            else f'base{index + 1}'
        )
        parts.append(overlay_video_filter(config, overlay))
        parts.append(
            f'[{current}][{overlay.name}_loop]'
            f'overlay=(W-w)/2:(H-h)/2:eof_action=repeat[{output}]'
        )
        current = output
    if image_input is not None:
        parts.append(f'[{image_input}:v]setpts=PTS-STARTPTS[image_live];')
        parts.append(
            f'[{current}][image_live]overlay=(W-w)/2:(H-h)/2:eof_action=pass[video]'
        )
    return ''.join(parts)


def overlay_video_filter(config: Streamo, overlay: VideoOverlay) -> str:
    width, height = video_size(config)
    fade_out_start = max(0.0, overlay.duration - overlay.fade)
    loop_frames = max(1, round(overlay.interval * config.video_frame_rate))
    return (
        f'[{overlay.input_index}:v]'
        f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
        f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,'
        f'fps={config.video_frame_rate},format=rgba,'
        f'trim=duration={overlay.duration:.6f},'
        'setpts=PTS-STARTPTS,'
        f'fade=t=in:st=0:d={overlay.fade:.6f}:alpha=1,'
        f'fade=t=out:st={fade_out_start:.6f}:d={overlay.fade:.6f}:alpha=1'
        f'[{overlay.name}_visible];'
        f'[{overlay.gap_index}:v]format=rgba,setpts=PTS-STARTPTS[{overlay.name}_gap];'
        f'[{overlay.name}_visible][{overlay.name}_gap]concat=n=2:v=1:a=0,'
        f'loop=loop=-1:size={loop_frames}:start=0,'
        f'setpts=N/FRAME_RATE/TB[{overlay.name}_loop];'
    )


def video_size(config: Streamo) -> tuple[int, int]:
    width, height = config.video_resolution.lower().split('x', maxsplit=1)
    return int(width), int(height)


AUDIO_CODECS = {
    'aac': 'aac',
    'mp3': 'libmp3lame',
    'opus': 'libopus',
    'vorbis': 'libvorbis',
}


VIDEO_CODECS = {
    'h264': 'libx264',
    'hevc': 'libx265',
    'av1': 'libaom-av1',
}
