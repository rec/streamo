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

from .config import Streamo
from .control import ControlController, RuntimeState
from .images import ImageFrameProducer, ImageScheduler, write_image_frames
from .programs import update_bitrate


@dataclass(frozen=True)
class VideoOverlay:
    name: str
    image: Path
    input_index: int
    gap_index: int
    interval: float
    duration: float
    fade: float


def stream(
    config: Streamo, controller: ControlController, *, preview: bool = False
) -> int:
    state = controller.state
    requested_stop = False
    result = 1
    image_read: int | None = None
    image_write: int | None = None
    image_thread: threading.Thread | None = None
    preview_process: subprocess.Popen[bytes] | None = None
    if config.image_interval > 0:
        image_read, image_write = os.pipe()
    try:
        if preview:
            preview_process = subprocess.Popen(
                ffplay_command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
        command = ffmpeg_command(config, image_pipe=image_read, preview=preview)
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
    except OSError:
        if image_read is not None:
            os.close(image_read)
        if image_write is not None:
            os.close(image_write)
        if preview_process is not None:
            process.terminate(preview_process)
        raise
    if preview_process is not None and preview_process.stdin is not None:
        preview_process.stdin.close()
    if image_read is not None:
        os.close(image_read)
    if image_write is not None:
        image_stream = os.fdopen(image_write, "wb")
        image_thread = threading.Thread(
            target=write_image_frames,
            args=(image_stream, image_frame_producer(config)),
            name="StreamoImageFrames",
            daemon=True,
        )
        image_thread.start()
    ffmpeg_output = process.capture_stderr(
        ffmpeg,
        lambda line: update_bitrate(state, line),
        thread_name="StreamoProcessOutput",
    )
    try:
        state.set_ffmpeg(alive=True)
        state.set_state("streaming")
        with sounddevice.InputStream(
            callback=_audio_callback(config, ffmpeg, state),
            channels=config.required_channels,
            device=config.device_name,
            dtype="float32",
            samplerate=config.sample_rate,
        ):
            while ffmpeg.poll() is None:
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
                process.report_failed_process(command, ffmpeg_output)
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
    config: Streamo, *, image_pipe: int | None = None, preview: bool = False
) -> list[str]:
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
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        config.video.as_posix(),
    ]
    if config.title_card is not None:
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
    if config.image_interval > 0:
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
    else:
        command.extend(["-map", "1:v:0"])
    command.extend(
        [
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "animation",
            "-b:v",
            config.video_bitrate,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(config.video_frame_rate),
            "-s",
            config.video_resolution,
            "-c:a",
            "aac",
            "-b:a",
            config.audio_bitrate,
            "-ar",
            str(config.sample_rate),
            "-ac",
            "2",
        ]
    )
    if preview:
        command.extend(["-f", "nut", "pipe:1"])
    else:
        command.extend(["-f", "flv", config.rtmp_url])
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


def image_frame_producer(config: Streamo) -> ImageFrameProducer:
    width, height = video_size(config)
    return ImageFrameProducer(
        ImageScheduler(config.image_dir),
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
