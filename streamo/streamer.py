import os
import subprocess
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote, unquote_plus

from pydantic import SecretStr
from reccy.runtime import process
from reccy.runtime.logging import get_logger

from .audio import AudioCapture
from .config import Streamo
from .control import ControlController
from .images import ImageFrameProducer, ImageScheduler, write_image_frames
from .programs import update_bitrate
from .services import (
    FfmpegDestination,
    FfmpegOutput,
    StreamingServiceAdapter,
    StreamingServiceConfiguration,
    ingest_output,
    tee_escape,
)

LOGGER = get_logger(__name__)
LOCAL_DISPLAY_URL = 'udp://127.0.0.1:23000?pkt_size=1316'
DRM_STATUS_ROOT = Path('/sys/class/drm')
DISPLAY_POLL_INTERVAL = 1.0
PLAYER_SHUTDOWN_TIMEOUT = 5.0


@dataclass(frozen=True)
class VideoOverlay:
    name: str
    image: Path
    input_index: int
    gap_index: int
    interval: float
    duration: float
    fade: float


class LocalDisplayController:
    def __init__(self, status_root: Path = DRM_STATUS_ROOT) -> None:
        self.status_root = status_root
        self.connected = False
        self.player: subprocess.Popen[bytes] | None = None

    def update(self) -> None:
        if self.player is not None and self.player.poll() is not None:
            LOGGER.warning('Local display player exited')
            self.player = None
        connected = drm_connected(self.status_root)
        if connected == self.connected:
            return
        self.connected = connected
        if connected:
            self.start_player()
        else:
            self.stop_player()

    def close(self) -> None:
        self.connected = False
        self.stop_player()

    def start_player(self) -> None:
        if self.player is not None:
            return
        try:
            self.player = subprocess.Popen(
                local_ffplay_command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, 'SDL_VIDEODRIVER': 'KMSDRM'},
            )
        except OSError as error:
            LOGGER.error('Could not start local display player: %s', error)

    def stop_player(self) -> None:
        if self.player is None:
            return
        player = self.player
        self.player = None
        try:
            player.terminate()
            player.wait(timeout=PLAYER_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait()
        except OSError:
            pass


def stream(
    config: Streamo,
    controller: ControlController,
    service: StreamingServiceAdapter,
    *,
    initial_image_paths: set[Path],
    preview: bool = False,
) -> int:
    state = controller.state
    ffmpeg: subprocess.Popen[bytes] | None = None
    try:
        with ExitStack() as resources:
            producer = (
                image_frame_producer(config, initial_image_paths)
                if config.streaming_service.encoding.video is not None
                and config.image_interval > 0
                else None
            )
            prepared = None
            output = None
            if not preview:
                resources.callback(service.finish)
                prepared = service.prepare(config.streaming_service.metadata)
                state.configure_service(service)
                output = local_display_output(config, service.output(prepared))
            image_input = None
            image_stream = None
            if producer is not None:
                read_fd, write_fd = os.pipe()
                image_input = resources.enter_context(os.fdopen(read_fd, 'rb'))
                image_stream = os.fdopen(write_fd, 'wb')
                resources.callback(image_stream.close)
            preview_process = None
            if preview:
                preview_process = subprocess.Popen(
                    ffplay_command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
                )
                resources.callback(process.terminate, preview_process)
                if preview_process.stdin is not None:
                    resources.callback(preview_process.stdin.close)
            command = ffmpeg_command(
                config,
                output=output,
                image_pipe=None if image_input is None else image_input.fileno(),
                preview=preview,
            )
            ffmpeg = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=preview_process.stdin if preview_process else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=() if image_input is None else (image_input.fileno(),),
                bufsize=0,
            )
            resources.callback(process.terminate, ffmpeg)
            assert ffmpeg.stdin is not None
            resources.callback(ffmpeg.stdin.close)
            audio = AudioCapture(
                config.device_name,
                config.channel,
                config.sample_rate,
                ffmpeg.stdin,
                state,
            )
            resources.callback(audio.close_capture)
            local_display = None
            if (
                not preview
                and config.local_display
                and config.streaming_service.encoding.video
            ):
                local_display = LocalDisplayController()
                resources.callback(local_display.close)
            if image_stream is not None and producer is not None:
                image_thread = threading.Thread(
                    target=write_image_frames,
                    args=(image_stream, producer),
                    name='StreamoImageFrames',
                    daemon=True,
                )
                image_thread.start()
                resources.callback(image_thread.join, 5)
                # Terminate the reader before waiting for the image writer.
                resources.callback(process.terminate, ffmpeg)
            if preview_process is not None and preview_process.stdin is not None:
                preview_process.stdin.close()
            if image_input is not None:
                image_input.close()
            ffmpeg_output = process.capture_stderr(
                ffmpeg,
                lambda line: update_bitrate(state, line),
                thread_name='StreamoProcessOutput',
            )
            state.set_ffmpeg(alive=True)
            state.set_state('muted' if state.is_muted() else 'streaming')
            if prepared is not None:
                service.publish(prepared)
            next_display_poll = 0.0
            while ffmpeg.poll() is None:
                if should_stop(controller) or (
                    preview_process is not None and preview_process.poll() is not None
                ):
                    state.set_state('stopping')
                    return 0
                audio.update()
                if local_display is not None and time.monotonic() >= next_display_poll:
                    local_display.update()
                    next_display_poll = time.monotonic() + DISPLAY_POLL_INTERVAL
                time.sleep(0.01)
            returncode = ffmpeg.wait()
            if returncode:
                diagnostic_output = ffmpeg_output.text()
                if output is not None:
                    command = redacted_ffmpeg_command(command, output)
                    diagnostic_output = redacted_ffmpeg_stderr(
                        diagnostic_output, output, service.service
                    )
                process.report_failed_command(command, None, diagnostic_output)
                state.set_error(f'FFmpeg exited with {returncode}')
            return returncode
    except KeyboardInterrupt:
        return 0
    finally:
        state.set_ffmpeg(
            alive=False, returncode=None if ffmpeg is None else ffmpeg.returncode
        )
        if state.snapshot()['state'] != 'failed':
            state.set_state('stopped')


def should_stop(controller: ControlController) -> bool:
    while not controller.commands.empty():
        command = controller.commands.get_nowait()
        if command.name == 'stop':
            return True
    return False


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


def ffplay_command() -> list[str]:
    return [
        'ffplay',
        '-hide_banner',
        '-loglevel',
        'warning',
        '-autoexit',
        '-f',
        'nut',
        'pipe:0',
    ]


def local_ffplay_command() -> list[str]:
    return [
        'ffplay',
        '-hide_banner',
        '-loglevel',
        'warning',
        '-fflags',
        'nobuffer',
        '-flags',
        'low_delay',
        '-framedrop',
        '-fs',
        '-an',
        '-f',
        'mpegts',
        LOCAL_DISPLAY_URL,
    ]


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


def drm_connected(status_root: Path) -> bool:
    for status_path in status_root.glob('*/status'):
        try:
            if status_path.read_text().strip() == 'connected':
                return True
        except OSError:
            continue
    return False


def redacted_ffmpeg_command(command: list[str], output: FfmpegOutput) -> list[str]:
    return command[: -len(output.arguments)] + output.redacted_arguments()


def redacted_ffmpeg_stderr(
    text: str, output: FfmpegOutput, service: StreamingServiceConfiguration
) -> str:
    sensitive = {d.url for d in output.destinations if d.secret_url}
    if service.ingest is not None:
        sensitive.update(
            v.get_secret_value()
            for v in service.ingest.model_dump().values()
            if isinstance(v, SecretStr) and v.get_secret_value()
        )
    variants = {
        v
        for s in sensitive
        for v in (s, unquote(s), unquote_plus(s), quote(s, safe=''), quote_plus(s))
        if v
    }
    variants.update(tee_escape(v) for v in list(variants))
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    return text


def image_frame_producer(
    config: Streamo, initial_image_paths: set[Path]
) -> ImageFrameProducer:
    width, height = video_size(config)
    return ImageFrameProducer(
        ImageScheduler(
            config.image_dir,
            initial_paths=initial_image_paths,
            session_weight=config.current_session_image_weight,
        ),
        width=width,
        height=height,
        frame_rate=config.video_frame_rate,
        interval=config.image_interval,
        duration=config.image_duration,
        fade=config.image_fade,
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
    width, height = video_size(config)
    parts = [
        '[1:v]'
        f'scale={width}:{height},fps={config.video_frame_rate},'
        'format=yuv420p[base];'
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
