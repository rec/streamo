import threading
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel, Field

from .providers import StreamingServiceAdapter, endpoint_host


class HealthWarnings(BaseModel, frozen=True):
    silence_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    silence_level_db: float = Field(default=-60, ge=-120, le=0, allow_inf_nan=False)
    clipping_seconds: float = Field(default=2, ge=0, allow_inf_nan=False)
    output_stall_seconds: float = Field(default=10, ge=0, allow_inf_nan=False)


class Incident(BaseModel, frozen=True):
    sequence: int
    timestamp: float
    component: str
    state: Literal['incident', 'recovery']
    detail: str
    audio_dropped_frames: int


class RuntimeState:
    def __init__(self, warnings: HealthWarnings | None = None) -> None:
        self._lock = threading.Lock()
        self.state = 'starting'
        self.muted = False
        self.ffmpeg_alive = False
        self.ffmpeg_returncode: int | None = None
        self.audio_frames = 0
        self.audio_seconds = 0.0
        self.last_audio_at: float | None = None
        self.left_level_db: float | None = None
        self.right_level_db: float | None = None
        self.clipping = False
        self.output_bitrate_kbps: float | None = None
        self.last_error: str | None = None
        self.service: str | None = None
        self.endpoint_host: str | None = None
        self.capabilities: list[str] = []
        self.remote_health: dict[str, object] | None = None
        self.remote_health_error: str | None = None
        self.remote_health_updated_at: float | None = None
        self.audio_error: str | None = None
        self.audio_dropped_frames = 0
        self.audio_error_count = 0
        self.audio_last_error: str | None = None
        self.publish_requested = False
        self.encoder_attempts = 0
        self.next_retry_at: float | None = None
        self.publish_error: str | None = None
        self.last_publish_error: str | None = None
        self.last_output_progress_at: float | None = None
        self.progress_started: float | None = None
        self.progress_updated: float | None = None
        self.output_time_us = 0
        self.warnings = warnings or HealthWarnings()
        self.incidents: deque[Incident] = deque(maxlen=100)
        self.incident_sequence = 0
        self.active_warnings: set[str] = set()
        self.silence_since: float | None = None
        self.clipping_since: float | None = None
        self.last_audio_monotonic: float | None = None
        self.encoder_started_at: float | None = None
        self.last_audio_loss_at: float | None = None
        self.audio_loss_active = False
        self.health_enabled = False
        self.health_checked_at = time.monotonic()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                'state': self.state,
                'muted': self.muted,
                'ffmpeg_alive': self.ffmpeg_alive,
                'ffmpeg_returncode': self.ffmpeg_returncode,
                'audio_frames': self.audio_frames,
                'audio_seconds': self.audio_seconds,
                'last_audio_at': self.last_audio_at,
                'left_level_db': self.left_level_db,
                'right_level_db': self.right_level_db,
                'clipping': self.clipping,
                'output_bitrate_kbps': self.output_bitrate_kbps,
                'last_error': self.last_error,
                'publish_requested': self.publish_requested,
                'encoder_attempts': self.encoder_attempts,
                'next_retry_at': self.next_retry_at,
                'publish_error': self.publish_error,
                'last_publish_error': self.last_publish_error,
                'last_output_progress_at': self.last_output_progress_at,
                'warnings': sorted(self.active_warnings),
                'incident_sequence': self.incident_sequence,
                'audio_error': self.audio_error,
                'audio_last_error': self.audio_last_error,
                'audio_error_count': self.audio_error_count,
                'audio_dropped_frames': self.audio_dropped_frames,
                'service': self.service,
                'endpoint_host': self.endpoint_host,
                'capabilities': list(self.capabilities),
                'remote_health_error': self.remote_health_error,
                'remote_health_updated_at': self.remote_health_updated_at,
                'remote_health': (
                    None if self.remote_health is None else dict(self.remote_health)
                ),
            }

    def configure_service(self, adapter: StreamingServiceAdapter) -> None:
        with self._lock:
            self.service = adapter.service.service
            self.endpoint_host = endpoint_host(adapter.service)
            self.capabilities = [c.value for c in adapter.capabilities]
            self.health_enabled = 'health' in self.capabilities
            self.health_checked_at = time.monotonic()

    def set_audio_health(self, error: str | None, dropped_frames: int) -> None:
        with self._lock:
            previous_drops = self.audio_dropped_frames
            self.audio_dropped_frames = dropped_frames
            if error != self.audio_error:
                self._event(
                    'audio_capture',
                    error is not None,
                    error or 'Audio capture recovered',
                )
            if dropped_frames > previous_drops:
                self.last_audio_loss_at = time.monotonic()
                if not self.audio_loss_active:
                    self._event('audio_loss', True, 'Audio frames are being discarded')
                    self.audio_loss_active = True
            elif (
                self.audio_loss_active
                and error is None
                and self.last_audio_loss_at is not None
                and time.monotonic() - self.last_audio_loss_at >= 1
            ):
                self._event(
                    'audio_loss',
                    False,
                    'Audio delivery recovered; see cumulative dropped-frame count',
                )
                self.audio_loss_active = False
            if error and error != self.audio_error:
                self.audio_error_count += 1
                self.audio_last_error = error
            self.audio_error = error

    def set_remote_health(
        self, health: dict[str, object] | None, error: str | None = None
    ) -> None:
        with self._lock:
            if error != self.remote_health_error:
                self._event(
                    'provider_health',
                    error is not None,
                    error or 'Provider health query recovered',
                )
            self.remote_health = health
            self.remote_health_error = error
            self.remote_health_updated_at = time.time()
            self.health_checked_at = time.monotonic()

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def set_ffmpeg(self, *, alive: bool, returncode: int | None = None) -> None:
        with self._lock:
            self.ffmpeg_alive = alive
            self.ffmpeg_returncode = returncode

    def begin_encoder_attempt(self) -> None:
        with self._lock:
            self.encoder_attempts += 1
            self.next_retry_at = None
            self.output_bitrate_kbps = None
            self.last_output_progress_at = None
            self.progress_started = self.progress_updated = None
            self.output_time_us = 0
            self.state = 'starting'
            self.encoder_started_at = time.monotonic()

    def set_publish_requested(self, requested: bool) -> None:
        with self._lock:
            self.publish_requested = requested
            if not requested:
                self.next_retry_at = None

    def publish_failed(self, message: str, retry_delay: float | None) -> None:
        with self._lock:
            if message != self.publish_error:
                self._event('encoder', True, message)
            self.publish_error = self.last_publish_error = self.last_error = message
            self.next_retry_at = (
                None if retry_delay is None else time.time() + retry_delay
            )
            self.state = 'failed' if retry_delay is None else 'recovering'
            self.output_bitrate_kbps = None

    def record_output_progress(self, output_time_us: int) -> None:
        with self._lock:
            if output_time_us <= self.output_time_us:
                return
            now = time.monotonic()
            if self.progress_updated is None or now - self.progress_updated > 10:
                self.progress_started = now
            self.output_time_us = output_time_us
            self.progress_updated = now
            self.last_output_progress_at = time.time()
            if self.publish_error is not None:
                self._event(
                    'encoder',
                    False,
                    'Encoder output resumed; provider delivery is separate',
                )
            self.publish_error = None
            self.state = 'muted' if self.muted else 'streaming'

    def output_is_stable(self) -> bool:
        with self._lock:
            return (
                self.progress_started is not None
                and self.progress_updated is not None
                and self.progress_updated - self.progress_started >= 60
                and time.monotonic() - self.progress_updated <= 10
            )

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            if muted != self.muted:
                self.silence_since = self.clipping_since = None
            self.muted = muted
            if self.state in {'streaming', 'muted'}:
                self.state = 'muted' if muted else 'streaming'

    def is_muted(self) -> bool:
        with self._lock:
            return self.muted

    def record_audio(
        self,
        *,
        frames: int,
        sample_rate: int,
        left_level_db: float,
        right_level_db: float,
        clipping: bool,
    ) -> None:
        with self._lock:
            self.audio_frames += frames
            self.audio_seconds = self.audio_frames / sample_rate
            self.last_audio_at = time.time()
            self.left_level_db = left_level_db
            self.right_level_db = right_level_db
            self.clipping = clipping
            now = time.monotonic()
            recent = (
                self.last_audio_monotonic is not None
                and now - self.last_audio_monotonic <= 3
            )
            self.last_audio_monotonic = now
            silent = (
                not self.muted
                and max(left_level_db, right_level_db) <= self.warnings.silence_level_db
            )
            if not silent:
                self.silence_since = None
            elif not recent or self.silence_since is None:
                self.silence_since = now
            if not clipping or self.muted:
                self.clipping_since = None
            elif not recent or self.clipping_since is None:
                self.clipping_since = now

    def events_since(self, after: int) -> dict[str, object]:
        with self._lock:
            oldest = (
                self.incidents[0].sequence
                if self.incidents
                else self.incident_sequence + 1
            )
            return {
                'events': [
                    e.model_dump() for e in self.incidents if e.sequence > after
                ],
                'oldest_sequence': oldest,
                'latest_sequence': self.incident_sequence,
                'history_lost': after < oldest - 1 or after > self.incident_sequence,
            }

    def evaluate_warnings(self) -> None:
        with self._lock:
            now = time.monotonic()
            recent = (
                self.last_audio_monotonic is not None
                and now - self.last_audio_monotonic <= 3
            )
            for name, since, seconds in (
                ('silence', self.silence_since, self.warnings.silence_seconds),
                ('clipping', self.clipping_since, self.warnings.clipping_seconds),
            ):
                active = (
                    recent
                    and not self.muted
                    and seconds > 0
                    and since is not None
                    and now - since >= seconds
                )
                self._warning(
                    name,
                    active,
                    f'Sustained {name} in captured audio',
                    f'{name.capitalize()} warning cleared',
                )
            progress = (
                self.progress_updated
                if self.progress_updated is not None
                else self.encoder_started_at
            )
            stalled = (
                self.ffmpeg_alive
                and progress is not None
                and self.warnings.output_stall_seconds > 0
                and now - progress >= self.warnings.output_stall_seconds
            )
            self._warning(
                'output_stalled',
                stalled,
                'Encoder output progress has stopped',
                'Output progress resumed'
                if self.ffmpeg_alive
                else 'Output warning cleared because encoder stopped',
            )
            self._warning(
                'provider_health_stale',
                self.health_enabled and now - self.health_checked_at >= 90,
                'Provider health has not refreshed for 90 seconds',
                'Provider health freshness restored',
            )

    def set_output_bitrate(self, bitrate_kbps: float | None) -> None:
        with self._lock:
            self.output_bitrate_kbps = bitrate_kbps

    def _warning(self, component: str, active: bool, detail: str, cleared: str) -> None:
        if active == (component in self.active_warnings):
            return
        if active:
            self.active_warnings.add(component)
        else:
            self.active_warnings.discard(component)
        self._event(component, active, detail if active else cleared)

    def _event(self, component: str, active: bool, detail: str) -> None:
        """Record a transition while the caller holds the state lock."""
        self.incident_sequence += 1
        self.incidents.append(
            Incident(
                sequence=self.incident_sequence,
                timestamp=time.time(),
                component=component,
                state='incident' if active else 'recovery',
                detail=detail,
                audio_dropped_frames=self.audio_dropped_frames,
            )
        )
