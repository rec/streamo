import os
import time
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from streamo.audio import AudioCapture
from streamo.control import RuntimeState


def test_stalled_output_drops_old_audio_and_recovers(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    state = RuntimeState()
    with (
        os.fdopen(read_fd, 'rb', buffering=0) as source,
        os.fdopen(write_fd, 'wb', buffering=0) as output,
    ):
        audio = AudioCapture('unused', 2, 48000, output, state)
        audio.capture = mock.Mock(active=True)
        os.set_blocking(read_fd, False)
        while True:
            try:
                os.write(write_fd, b'\0' * 4096)
            except BlockingIOError:
                break
        audio.last_write = time.monotonic() - 2
        block = np.full((1024, 3), 0.25, dtype=np.float32)
        for _ in range(100):
            audio.callback(block, len(block), None, '')
        audio.update()
        assert state.snapshot()['audio_dropped_frames'] > 0
        assert state.snapshot()['audio_error'] is not None
        while source.read(65536):
            pass
        audio.update()
        assert state.snapshot()['audio_error'] is None
        assert state.snapshot()['audio_last_error'] is not None
        captured = bytearray()
        for _ in range(48):
            audio.callback(block, len(block), None, '')
            audio.update()
            captured.extend(source.read(65536) or b'')
        samples = np.frombuffer(captured, dtype=np.float32).reshape(-1, 2)
        assert len(samples) >= 48000
        assert np.all(samples == 0.25)
        with wave.open(str(tmp_path / 'recovered.wav'), 'wb') as recording:
            recording.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            recording.writeframes((samples * 32767).astype('<i2').tobytes())


def test_capture_failure_is_reported_and_retried(tmp_path: Path) -> None:
    with (tmp_path / 'output').open('wb') as output:
        state = RuntimeState()
        audio = AudioCapture('missing', 1, 48000, output, state)
        with mock.patch('streamo.audio.sounddevice.InputStream', side_effect=OSError):
            audio.update()
        assert 'unavailable' in str(state.snapshot()['audio_error'])
        assert audio.retry_at > time.monotonic()
        audio.retry_at = 0
        with mock.patch('streamo.audio.sounddevice.InputStream') as capture:
            audio.update()
            capture.return_value.start.assert_called_once()


def test_partial_writes_preserve_sample_alignment(tmp_path: Path) -> None:
    with (tmp_path / 'output').open('wb') as output:
        audio = AudioCapture('unused', 1, 48000, output, RuntimeState())
        audio.capture = mock.Mock(active=True)
        audio.pending = b'abcdefgh'
        with mock.patch('streamo.audio.os.write', return_value=3):
            audio.update()
        assert audio.pending == b'defgh'
