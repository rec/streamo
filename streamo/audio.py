import os
import queue
import time
from typing import IO

import numpy as np
import sounddevice
from reccy.runtime.retry import RetryPolicy, RetrySchedule

from .runtime import RuntimeState


class AudioCapture:
    """Keep capture independent of FFmpeg; retain at most one second of audio."""

    def __init__(
        self,
        device: str,
        channel: int,
        sample_rate: int,
        output: IO[bytes] | None,
        state: RuntimeState,
    ) -> None:
        self.device = device
        self.channel = channel
        self.sample_rate = sample_rate
        self.output = output
        self.state = state
        self.blocks: queue.Queue[tuple[np.ndarray, str]] = queue.Queue(
            maxsize=max(1, sample_rate // 1024)
        )
        self.pending = b''
        self.dropped_frames = 0
        self.overflow_frames = 0
        self.last_capture = time.monotonic()
        self.last_write = self.last_capture
        self.open_retry = RetrySchedule(RetryPolicy(delay=5), clock=time.monotonic)
        self.capture_retry = RetrySchedule(RetryPolicy(delay=1), clock=time.monotonic)
        self.capture: sounddevice.InputStream | None = None
        self.error: str | None = None
        if output is not None:
            os.set_blocking(output.fileno(), False)

    def attach_output(self, output: IO[bytes] | None) -> None:
        """Start a new encoder on fresh, complete audio frames."""
        self.dropped_frames += (len(self.pending) + 7) // 8
        self.pending = b''
        while True:
            try:
                block, _ = self.blocks.get_nowait()
            except queue.Empty:
                break
            self.dropped_frames += len(block)
        if output is not None:
            os.set_blocking(output.fileno(), False)
        self.output = output
        self.last_write = time.monotonic()

    def callback(
        self, data: np.ndarray, frames: int, timing: object, status: object
    ) -> None:
        self.last_capture = time.monotonic()
        block = (data[:, self.channel - 1 : self.channel + 1].copy(), str(status))
        try:
            self.blocks.put_nowait(block)
        except queue.Full:
            try:
                discarded, _ = self.blocks.get_nowait()
                self.overflow_frames += len(discarded)
            except queue.Empty:
                pass
            self.blocks.put_nowait(block)

    def update(self) -> None:
        now = time.monotonic()
        status = ''
        if self.capture is not None and (
            not self.capture.active or now - self.last_capture > 3
        ):
            self.error = 'Audio capture stopped; retrying'
            self.close_capture()
            self.capture_retry.failed()
        if (
            self.capture is None
            and self.capture_retry.seconds_until_attempt() == 0
            and self.open_retry.begin_attempt()
        ):
            try:
                self.capture = sounddevice.InputStream(
                    device=self.device,
                    channels=self.channel + 1,
                    samplerate=self.sample_rate,
                    dtype='float32',
                    blocksize=1024,
                    callback=self.callback,
                )
                self.capture.start()
                self.last_capture = now
                self.open_retry.reset()
                self.capture_retry.reset()
                self.capture_retry.begin_attempt()
            except (sounddevice.PortAudioError, OSError, ValueError):
                self.error = 'Audio device unavailable; retrying'
                self.close_capture()
                self.open_retry.failed()
        if not self.pending:
            try:
                block, status = self.blocks.get_nowait()
            except queue.Empty:
                block = None
                status = ''
            if block is not None:
                if self.state.is_muted():
                    block.fill(0)
                self.state.record_audio(
                    frames=len(block),
                    sample_rate=self.sample_rate,
                    left_level_db=level_db(block[:, 0]),
                    right_level_db=level_db(block[:, 1]),
                    clipping=bool(np.max(np.abs(block)) >= 1),
                )
                self.pending = block.tobytes()
                if status:
                    self.error = f'Audio capture: {status}'
        if self.pending and self.output is None:
            self.dropped_frames += (len(self.pending) + 7) // 8
            self.pending = b''
            self.error = 'Encoder unavailable; discarding captured audio'
        if self.pending and self.output is not None:
            try:
                written = os.write(self.output.fileno(), self.pending)
            except BlockingIOError:
                if now - self.last_write > 1:
                    self.error = 'FFmpeg audio input stalled; discarding old audio'
            except OSError:
                self.error = 'FFmpeg audio input closed'
            else:
                self.pending = self.pending[written:]
                self.last_write = now
                if not status and self.capture is not None:
                    self.error = None
        self.state.set_audio_health(
            self.error, self.dropped_frames + self.overflow_frames
        )

    def close_capture(self) -> None:
        if self.capture is not None:
            capture, self.capture = self.capture, None
            capture.close()


def level_db(samples: np.ndarray) -> float:
    peak = float(np.max(np.abs(samples)))
    return float(20 * np.log10(peak)) if peak > 0 else -120.0
