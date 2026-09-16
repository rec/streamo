from .config import Streamo
from .providers import FfmpegDestination, FfmpegOutput, ingest_output

LOCAL_DISPLAY_URL = 'udp://127.0.0.1:23000?pkt_size=1316'


def ffmpeg_command(
    config: Streamo,
    *,
    output: FfmpegOutput | None = None,
    image_pipe: int | None = None,
    preview: bool = False,
) -> list[str]:
    encoding = config.streaming_service.encoding
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
    if encoding.video is not None and config.live_overlays:
        if image_pipe is None:
            raise ValueError('overlay pipe is required when live_overlays is enabled')
        command.extend(
            [
                '-f',
                'rawvideo',
                '-pixel_format',
                'rgba',
                '-video_size',
                encoding.video.resolution,
                '-framerate',
                str(config.video_frame_rate),
                '-i',
                f'pipe:{image_pipe}',
                '-filter_complex',
                overlay_filter(config),
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


def overlay_filter(config: Streamo) -> str:
    encoding = config.streaming_service.encoding.video
    assert encoding is not None
    width, height = encoding.resolution.lower().split('x')
    return (
        f'[1:v]scale={width}:{height},fps={encoding.frame_rate},format=yuv420p[base];'
        '[2:v]setpts=PTS-STARTPTS[live];'
        '[base][live]overlay=0:0:eof_action=pass[video]'
    )


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
