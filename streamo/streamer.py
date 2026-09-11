import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice
from reccy.runtime import process
from reccy.runtime.logging import get_logger

from .config import Streamo
from .control import ControlController, RuntimeState
from .images import ImageFrameProducer, ImageScheduler, write_image_frames
from .programs import update_bitrate
from .services import (
    FfmpegDestination,
    FfmpegOutput,
    StreamingServiceAdapter,
    ingest_output,
)

LOGGER = get_logger(__name__)
LOCAL_DISPLAY_URL = "udp://127.0.0.1:23000?pkt_size=1316"
DRM_STATUS_ROOT = Path("/sys/class/drm")
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
            LOGGER.warning("Local display player exited")
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
                env={**os.environ, "SDL_VIDEODRIVER": "KMSDRM"},
            )
        except OSError as error:
            LOGGER.error("Could not start local display player: %s", error)

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
    prepared = None
    service_output = None
    if not preview:
        prepared = service.prepare(config.streaming_service.metadata)
        state.configure_service(service)
        service_output = service.output(prepared)
    requested_stop = False
    result = 1
    image_read: int | None = None
    image_write: int | None = None
    image_thread: threading.Thread | None = None
    preview_process: subprocess.Popen[bytes] | None = None
    local_display: LocalDisplayController | None = None
    if (
        config.streaming_service.encoding.video is not None
        and config.image_interval > 0
    ):
        image_read, image_write = os.pipe()
    try:
        if preview:
            preview_process = subprocess.Popen(
                ffplay_command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
        command_output = (
            None if preview else local_display_output(config, service_output)
        )
        command = ffmpeg_command(
            config,
            output=command_output,
            image_pipe=image_read,
            preview=preview,
        )
        stdout = (
            preview_process.stdin if preview_process is not None else subprocess.DEVNULL
        )
        ffmpeg = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.PIPE,
            pass_fds=() if image_read is None else (image_read,),
        )
        if (
            not preview
            and config.local_display
            and config.streaming_service.encoding.video is not None
        ):
            local_display = LocalDisplayController()
    except OSError:
        if image_read is not None:
            os.close(image_read)
        if image_write is not None:
            os.close(image_write)
        if preview_process is not None:
            process.terminate(preview_process)
        if prepared is not None:
            service.finish()
        raise
    if preview_process is not None and preview_process.stdin is not None:
        preview_process.stdin.close()
    if image_read is not None:
        os.close(image_read)
    if image_write is not None:
        image_stream = os.fdopen(image_write, "wb")
        image_thread = threading.Thread(
            target=write_image_frames,
            args=(image_stream, image_frame_producer(config, initial_image_paths)),
            name="StreamoImageFrames",
            daemon=True,
        )
        image_thread.start()
    ffmpeg_output = process.capture_stderr(
        ffmpeg,
        lambda line: update_bitrate(state, line),
        thread_name="StreamoProcessOutput",
    )
    next_display_poll = 0.0
    try:
        state.set_ffmpeg(alive=True)
        state.set_state("streaming")
        if prepared is not None:
            service.publish(prepared)
        with sounddevice.InputStream(
            callback=_audio_callback(config, ffmpeg, state),
            channels=config.required_channels,
            device=config.device_name,
            dtype="float32",
            samplerate=config.sample_rate,
        ):
            while ffmpeg.poll() is None:
                if local_display is not None and time.monotonic() >= next_display_poll:
                    local_display.update()
                    next_display_poll = time.monotonic() + DISPLAY_POLL_INTERVAL
                if should_stop(controller) or (
                    preview_process is not None and preview_process.poll() is not None
                ):
                    requested_stop = True
                    state.set_state("stopping")
                    process.terminate(ffmpeg)
                    break
                time.sleep(0.05)
            returncode = ffmpeg.wait()
            state.set_ffmpeg(alive=False, returncode=returncode)
            if returncode and not requested_stop:
                diagnostic_command = command
                if not preview:
                    assert command_output is not None
                    diagnostic_command = redacted_ffmpeg_command(
                        command, command_output
                    )
                process.report_failed_process(diagnostic_command, ffmpeg_output)
            result = 0 if requested_stop else returncode
    except KeyboardInterrupt:
        state.set_state("stopping")
        process.terminate(ffmpeg)
        result = 0
    except BrokenPipeError:
        state.set_error("ffmpeg input pipe closed")
        result = 1
    finally:
        if ffmpeg.stdin is not None:
            ffmpeg.stdin.close()
        process.terminate(ffmpeg)
        if image_thread is not None:
            image_thread.join(timeout=5)
        if preview_process is not None:
            process.terminate(preview_process)
        if local_display is not None:
            local_display.close()
        if prepared is not None:
            service.finish()
        state.set_ffmpeg(alive=False, returncode=ffmpeg.returncode)
        if state.snapshot()["state"] != "failed":
            state.set_state("stopped")
    return result


def should_stop(controller: ControlController) -> bool:
    while not controller.commands.empty():
        command = controller.commands.get_nowait()
        if command.name == "stop":
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
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        "pipe:2",
        "-f",
        "f32le",
        "-ar",
        str(config.sample_rate),
        "-ac",
        "2",
        "-i",
        "pipe:0",
    ]
    if encoding.video is not None:
        assert config.video is not None
        command.extend(
            [
                "-re",
                "-stream_loop",
                "-1",
                "-i",
                config.video.as_posix(),
            ]
        )
    if encoding.video is not None and config.title_card is not None:
        overlays.append(
            VideoOverlay(
                name="title",
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
                "image pipe is required when participant images are enabled"
            )
        image_input = next_input
        width, height = video_size(config)
        command.extend(
            [
                "-f",
                "rawvideo",
                "-pixel_format",
                "rgba",
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                str(config.video_frame_rate),
                "-i",
                f"pipe:{image_pipe}",
            ]
        )
    if overlays or image_input is not None:
        command.extend(
            [
                "-filter_complex",
                overlay_filter(config, overlays, image_input=image_input),
                "-map",
                "[video]",
            ]
        )
    elif encoding.video is not None:
        command.extend(["-map", "1:v:0"])
    command.extend(["-map", "0:a:0"])
    if encoding.video is not None:
        command.extend(
            [
                "-c:v",
                VIDEO_CODECS[encoding.video.codec],
                "-b:v",
                encoding.video.bitrate,
                "-pix_fmt",
                encoding.video.pixel_format,
                "-r",
                str(encoding.video.frame_rate),
                "-s",
                encoding.video.resolution,
                "-g",
                str(
                    round(encoding.video.frame_rate * encoding.video.keyframe_interval)
                ),
            ]
        )
        if encoding.video.codec in {"h264", "hevc"}:
            command.extend(["-preset", "veryfast"])
        if encoding.video.codec == "h264":
            command.extend(["-tune", "animation"])
    command.extend(
        [
            "-c:a",
            AUDIO_CODECS[encoding.audio.codec],
            "-b:a",
            encoding.audio.bitrate,
            "-ar",
            str(encoding.audio.sample_rate),
            "-ac",
            str(encoding.audio.channels),
        ]
    )
    if preview:
        command.extend(["-f", "nut", "pipe:1"])
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
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-autoexit",
        "-f",
        "nut",
        "pipe:0",
    ]


def local_ffplay_command() -> list[str]:
    return [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-framedrop",
        "-fs",
        "-an",
        "-f",
        "mpegts",
        LOCAL_DISPLAY_URL,
    ]


def local_display_output(config: Streamo, output: FfmpegOutput | None) -> FfmpegOutput:
    output = output or ingest_output(config.streaming_service)
    if not config.local_display or config.streaming_service.encoding.video is None:
        return output
    return output.model_copy(
        update={
            "destinations": [
                *output.destinations,
                FfmpegDestination(
                    muxer="mpegts", url=LOCAL_DISPLAY_URL, secret_url=False
                ),
            ]
        }
    )


def drm_connected(status_root: Path) -> bool:
    for status_path in status_root.glob("*/status"):
        try:
            if status_path.read_text().strip() == "connected":
                return True
        except OSError:
            continue
    return False


def redacted_ffmpeg_command(command: list[str], output: FfmpegOutput) -> list[str]:
    return command[: -len(output.arguments)] + output.redacted_arguments()


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
            name="title",
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
        "-loop",
        "1",
        "-t",
        f"{overlay.duration:.6f}",
        "-i",
        overlay.image.as_posix(),
        "-f",
        "lavfi",
        "-t",
        f"{gap_duration:.6f}",
        "-i",
        (
            "color="
            f"c=black@0.0:s={width}x{height}:"
            f"r={config.video_frame_rate}:d={gap_duration:.6f}"
        ),
    ]


def title_filter(config: Streamo) -> str:
    assert config.title_card is not None
    return overlay_filter(
        config,
        [
            VideoOverlay(
                name="title",
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
        "[1:v]"
        f"scale={width}:{height},fps={config.video_frame_rate},"
        "format=yuv420p[base];"
    ]
    current = "base"
    for index, overlay in enumerate(overlays):
        output = (
            "video"
            if index == len(overlays) - 1 and image_input is None
            else f"base{index + 1}"
        )
        parts.append(overlay_video_filter(config, overlay))
        parts.append(
            f"[{current}][{overlay.name}_loop]"
            f"overlay=(W-w)/2:(H-h)/2:eof_action=repeat[{output}]"
        )
        current = output
    if image_input is not None:
        parts.append(f"[{image_input}:v]setpts=PTS-STARTPTS[image_live];")
        parts.append(
            f"[{current}][image_live]overlay=(W-w)/2:(H-h)/2:eof_action=pass[video]"
        )
    return "".join(parts)


def overlay_video_filter(config: Streamo, overlay: VideoOverlay) -> str:
    width, height = video_size(config)
    fade_out_start = max(0.0, overlay.duration - overlay.fade)
    loop_frames = max(1, round(overlay.interval * config.video_frame_rate))
    return (
        f"[{overlay.input_index}:v]"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={config.video_frame_rate},format=rgba,"
        f"trim=duration={overlay.duration:.6f},"
        "setpts=PTS-STARTPTS,"
        f"fade=t=in:st=0:d={overlay.fade:.6f}:alpha=1,"
        f"fade=t=out:st={fade_out_start:.6f}:d={overlay.fade:.6f}:alpha=1"
        f"[{overlay.name}_visible];"
        f"[{overlay.gap_index}:v]format=rgba,setpts=PTS-STARTPTS[{overlay.name}_gap];"
        f"[{overlay.name}_visible][{overlay.name}_gap]concat=n=2:v=1:a=0,"
        f"loop=loop=-1:size={loop_frames}:start=0,"
        f"setpts=N/FRAME_RATE/TB[{overlay.name}_loop];"
    )


def video_size(config: Streamo) -> tuple[int, int]:
    width, height = config.video_resolution.lower().split("x", maxsplit=1)
    return int(width), int(height)


def select_stereo_pair(config: Streamo, data: np.ndarray) -> np.ndarray:
    begin = config.channel - 1
    end = begin + 2
    if data.shape[1] < end:
        raise ValueError(
            f"device returned {data.shape[1]} channels; channel {config.channel} "
            "requires a stereo pair"
        )
    return np.ascontiguousarray(data[:, begin:end])


def _audio_callback(
    config: Streamo, process: subprocess.Popen[bytes], state: RuntimeState
) -> Callable[[np.ndarray, int, object, object], None]:
    def callback(
        indata: np.ndarray,
        frames: int,
        time: object,
        status: object,
    ) -> None:
        if process.stdin is not None:
            stereo = select_stereo_pair(config, indata)
            if state.is_muted():
                stereo = np.zeros_like(stereo)
            state.record_audio(
                frames=frames,
                sample_rate=config.sample_rate,
                left_level_db=level_db(stereo[:, 0]),
                right_level_db=level_db(stereo[:, 1]),
                clipping=bool(np.max(np.abs(stereo)) >= 1.0),
            )
            process.stdin.write(stereo.tobytes())

    return callback


def level_db(samples: np.ndarray) -> float:
    peak = float(np.max(np.abs(samples)))
    if peak <= 0:
        return -120.0
    return float(20 * np.log10(peak))


AUDIO_CODECS = {
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "vorbis": "libvorbis",
}

VIDEO_CODECS = {
    "h264": "libx264",
    "hevc": "libx265",
    "av1": "libaom-av1",
}
