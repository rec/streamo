import errno
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote, unquote_plus

from pydantic import SecretStr
from reccy.runtime import process
from reccy.runtime.logging import get_logger

from .audio import AudioCapture
from .composition import (
    LOCAL_DISPLAY_URL,
    ffmpeg_command,
    local_display_output,
    video_size,
)
from .config import Streamo
from .control import ControlController, HealthPoller
from .ffmpeg_progress import update_bitrate
from .images import ImageFrameProducer, ImageScheduler, write_image_frames
from .provider_config import StreamingServiceConfiguration
from .providers import FfmpegOutput, StreamingServiceAdapter, tee_escape

LOGGER = get_logger(__name__)
DRM_STATUS_ROOT = Path('/sys/class/drm')
DISPLAY_POLL_INTERVAL = 1.0
PLAYER_SHUTDOWN_TIMEOUT = 5.0


class LocalDisplayController:
    def __init__(self, status_root: Path = DRM_STATUS_ROOT) -> None:
        self.status_root = status_root
        self.connected = False
        self.player: subprocess.Popen[bytes] | None = None
        self.retry_at = 0.0
        self.player_output: process.OutputTail | None = None

    def update(self) -> None:
        if self.player is not None and self.player.poll() is not None:
            LOGGER.warning(
                'Local display player exited: %s',
                self.player_output.text() if self.player_output else '',
            )
            self.player = None
            self.retry_at = time.monotonic() + 5
        connected = drm_connected(self.status_root)
        if connected != self.connected:
            self.retry_at = 0
        self.connected = connected
        if connected and time.monotonic() >= self.retry_at:
            self.start_player()
        elif not connected:
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
                stderr=subprocess.PIPE,
                env={**os.environ, 'SDL_VIDEODRIVER': 'KMSDRM'},
            )
            self.player_output = process.capture_stderr(self.player)
        except OSError as error:
            LOGGER.error('Could not start local display player: %s', error)
            self.retry_at = time.monotonic() + 5

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
    recover = config.recover_publish and not preview
    state.set_publish_requested(not preview)
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
            audio = AudioCapture(
                config.device_name, config.channel, config.sample_rate, None, state
            )
            resources.callback(audio.close_capture)
            preview_process = None
            if preview:
                preview_process = subprocess.Popen(
                    ffplay_command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
                )
                resources.callback(process.terminate, preview_process)
                if preview_process.stdin is not None:
                    resources.callback(preview_process.stdin.close)
            local_display = None
            if (
                not preview
                and config.local_display
                and config.streaming_service.encoding.video
            ):
                local_display = LocalDisplayController()
                resources.callback(local_display.close)
            published = False

            def started() -> None:
                nonlocal published
                if prepared is not None and not published:
                    service.publish(prepared)
                    published = True
                    health = HealthPoller(service, state)
                    health.start()
                    resources.callback(health.close)

            failures = 0
            while not should_stop(controller):
                state.begin_encoder_attempt()
                try:
                    returncode, stopped = run_attempt(
                        config,
                        controller,
                        service,
                        audio,
                        producer,
                        output,
                        preview_process,
                        local_display,
                        started,
                    )
                except EncoderLaunchError as error:
                    returncode, stopped = 1, False
                    message = f'Could not launch FFmpeg (errno {error.errno})'
                    if not recover or error.errno not in RETRYABLE_LAUNCH_ERRORS:
                        state.publish_failed(message, None)
                        return returncode
                else:
                    if stopped:
                        return 0
                    message = f'FFmpeg exited with {returncode}'
                    if not recover:
                        if returncode:
                            state.publish_failed(message, None)
                        return returncode
                if state.output_is_stable():
                    failures = 0
                delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
                failures += 1
                state.publish_failed(message, delay)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if should_stop(controller):
                        return 0
                    audio.update()
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            return 0
    except KeyboardInterrupt:
        return 0
    finally:
        state.set_publish_requested(False)
        if state.snapshot()['state'] != 'failed':
            state.set_state('stopped')


class EncoderLaunchError(OSError):
    pass


def run_attempt(
    config: Streamo,
    controller: ControlController,
    service: StreamingServiceAdapter,
    audio: AudioCapture,
    producer: ImageFrameProducer | None,
    output: FfmpegOutput | None,
    preview_process: subprocess.Popen[bytes] | None,
    local_display: LocalDisplayController | None,
    started: Callable[[], None],
) -> tuple[int, bool]:
    state = controller.state
    with ExitStack() as resources:
        image_input = None
        image_stream = None
        if producer is not None:
            read_fd, write_fd = os.pipe()
            image_input = resources.enter_context(os.fdopen(read_fd, 'rb'))
            image_stream = os.fdopen(write_fd, 'wb')
            resources.callback(image_stream.close)
        command = ffmpeg_command(
            config,
            output=output,
            image_pipe=None if image_input is None else image_input.fileno(),
            preview=preview_process is not None,
        )
        try:
            ffmpeg = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=preview_process.stdin if preview_process else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=() if image_input is None else (image_input.fileno(),),
                bufsize=0,
            )
        except OSError as error:
            raise EncoderLaunchError(error.errno, 'Could not launch FFmpeg') from None
        threads: list[threading.Thread] = []
        resources.callback(close_encoder, ffmpeg, threads, controller)
        assert ffmpeg.stdin is not None
        audio.attach_output(ffmpeg.stdin)
        resources.callback(audio.attach_output, None)
        if image_stream is not None and producer is not None:
            image_thread = threading.Thread(
                target=write_image_frames,
                args=(image_stream, producer),
                name='streamOImageFrames',
                daemon=True,
            )
            image_thread.start()
            threads.append(image_thread)
        if preview_process is not None and preview_process.stdin is not None:
            preview_process.stdin.close()
        if image_input is not None:
            image_input.close()
        tail = process.OutputTail()
        if ffmpeg.stderr is not None:
            reader = threading.Thread(
                target=read_encoder_output,
                args=(ffmpeg, tail, controller),
                name='streamOEncoderOutput',
                daemon=True,
            )
            reader.start()
            threads.append(reader)
        state.set_ffmpeg(alive=True)
        started()
        next_display_poll = 0.0
        while ffmpeg.poll() is None:
            if should_stop(controller) or (
                preview_process is not None and preview_process.poll() is not None
            ):
                state.set_state('stopping')
                return 0, True
            audio.update()
            if local_display is not None and time.monotonic() >= next_display_poll:
                local_display.update()
                next_display_poll = time.monotonic() + DISPLAY_POLL_INTERVAL
            time.sleep(0.01)
        returncode = ffmpeg.wait()
        for thread in threads:
            thread.join(timeout=5)
        if returncode:
            diagnostics = tail.text()
            if output is not None:
                command = redacted_ffmpeg_command(command, output)
                diagnostics = redacted_ffmpeg_stderr(
                    diagnostics, output, service.service
                )
            process.report_failed_command(command, None, diagnostics)
        return returncode, False


def read_encoder_output(
    ffmpeg: subprocess.Popen[bytes],
    tail: process.OutputTail,
    controller: ControlController,
) -> None:
    assert ffmpeg.stderr is not None
    for line in ffmpeg.stderr:
        text = line.decode(errors='replace')
        tail.append(text)
        update_bitrate(controller.state, text)


def close_encoder(
    ffmpeg: subprocess.Popen[bytes],
    threads: list[threading.Thread],
    controller: ControlController,
) -> None:
    try:
        process.terminate(ffmpeg)
    finally:
        for thread in threads:
            thread.join(timeout=5)
        for pipe in (ffmpeg.stdin, ffmpeg.stderr):
            if pipe is not None:
                pipe.close()
        controller.state.set_ffmpeg(alive=False, returncode=ffmpeg.returncode)


def should_stop(controller: ControlController) -> bool:
    while not controller.commands.empty():
        command = controller.commands.get_nowait()
        if command.name == 'stop':
            return True
    return False


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


RETRY_DELAYS = (1, 2, 5, 10, 30)
RETRYABLE_LAUNCH_ERRORS = {
    errno.EAGAIN,
    errno.ENOMEM,
    errno.EMFILE,
    errno.ENFILE,
    errno.ETXTBSY,
}
