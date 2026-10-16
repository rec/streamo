"""Local recs subscription, isolated from encoder writes and provider control."""

import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO, Annotated, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from reccy.protocol import rpc
from recs.base.audio_stream import MAX_AUDIO_AGE_SECONDS, AudioClient, AudioHeader
from recs.daemon import gui_protocol, instances

from .audio import AudioOutput
from .closing import ClosingSequence
from .runtime import RuntimeState

OUTAGE_SECONDS = 30.0
RETRY_SECONDS = 0.5


class RecsInput(BaseModel, frozen=True):
    source: str = Field(min_length=1)
    channels: list[Annotated[int, Field(gt=0, strict=True)]] = Field(
        min_length=2, max_length=2
    )
    daemon: bool = False
    instance: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode='after')
    def validate_selection(self) -> Self:
        if self.daemon == (self.instance is not None):
            raise ValueError(
                'Select exactly one of daemon=true or a positive instance PID'
            )
        if len(set(self.channels)) != 2:
            raise ValueError(
                'Select two distinct physical channels in the desired order'
            )
        return self

    model_config = ConfigDict(extra='forbid')


class RecsInspection(BaseModel, frozen=True):
    instance: instances.InstanceIdentity
    sample_rate: int = Field(gt=0, le=192_000)
    available_channels: int = Field(gt=0)
    paused: bool
    muted: bool
    state: str
    enabled: bool


class RecsConnection:
    def __init__(self, settings: RecsInput) -> None:
        self.settings = settings
        self.target: instances.Target | None = None
        self.client: rpc.Client | None = None
        self.cancelled = threading.Event()

    def inspect(self) -> RecsInspection:
        if self.target is None:
            self.target = instances.resolve(
                instances.Selector(
                    daemon=self.settings.daemon,
                    instance=self.settings.instance,
                )
            )
            self.client = rpc.Client(
                self.target.control_endpoint, role='streamo-audio', timeout=1
            )
        assert self.client is not None
        if self.cancelled.is_set():
            raise InterruptedError('recs input stopped')
        caps = gui_protocol.CapabilitiesResult.model_validate(
            self.client.call('capabilities')
        )
        if not {'subscribe_audio', 'unsubscribe_audio', 'audio_status'} <= set(
            caps.commands
        ):
            raise ValueError('The selected recs does not support audio forwarding')
        if self.target.identity is not None and caps.instance != self.target.identity:
            raise ValueError('The selected recs instance identity changed')
        if self.settings.daemon and caps.instance.role != 'daemon':
            raise ValueError('The selected recs is not the daemon')
        if self.cancelled.is_set():
            raise InterruptedError('recs input stopped')
        status = gui_protocol.AudioStatusResult.model_validate(
            self.client.call('audio_status')
        )
        source = next(
            (s for s in status.sources if s.get('source') == self.settings.source), None
        )
        if source is None:
            raise ValueError('The requested hardware source is not present in recs')
        if self.cancelled.is_set():
            raise InterruptedError('recs input stopped')
        devices = gui_protocol.Devices.model_validate(self.client.call('list_devices'))
        device = next(
            (d for d in devices.devices if d.get('name') == self.settings.source), None
        )
        if device is None:
            raise ValueError('The requested device is unavailable in recs')
        inspection = RecsInspection.model_validate(
            {
                'instance': caps.instance,
                'sample_rate': device['sample_rate'],
                'available_channels': device['channels'],
                'paused': status.paused,
                'muted': source.get('muted', False),
                'state': source.get('state', 'unavailable'),
                'enabled': status.enabled,
            }
        )
        if max(self.settings.channels) > inspection.available_channels:
            raise ValueError('Requested channels exceed the selected recs source')
        return inspection

    def subscribe(self, inspection: RecsInspection) -> gui_protocol.AudioSubscribed:
        assert self.client is not None
        if self.cancelled.is_set():
            raise InterruptedError('recs input stopped')
        response = gui_protocol.AudioSubscribed.model_validate(
            self.client.call(
                'subscribe_audio',
                source=self.settings.source,
                channels=self.settings.channels,
            )
        )
        offer = response.subscription
        if (
            response.instance != inspection.instance
            or offer.instance_token != inspection.instance.start_token
            or offer.source != self.settings.source
            or offer.channels != self.settings.channels
            or offer.sample_rate != inspection.sample_rate
        ):
            self.cancel(offer.subscription_id)
            raise ValueError(
                'recs subscription does not match the selected instance, source, '
                'channels, or format'
            )
        return response

    def cancel(self, subscription_id: str) -> None:
        assert self.client is not None
        try:
            self.client.call(
                'unsubscribe_audio',
                source=self.settings.source,
                subscription_id=subscription_id,
            )
        except (OSError, ValueError):
            # The capture may already have exited. Its capability cannot be reused.
            pass

    def private_endpoint(self) -> bool:
        if os.name == 'nt':
            return True  # Existing recs named-pipe control security applies.
        if self.target is None:
            return False
        endpoint = Path(self.target.control_endpoint)
        return all(
            p.stat().st_uid == os.getuid() and not p.stat().st_mode & 0o022
            for p in (endpoint, endpoint.parent)
        )


class RecsReceiver:
    def __init__(self, settings: RecsInput) -> None:
        self.connection = RecsConnection(settings)
        self.lock = threading.Lock()
        self.stopped = self.connection.cancelled
        self.thread = threading.Thread(
            target=self.run, daemon=True, name='streamORecsAudio'
        )
        self.client: AudioClient | None = None
        self.received: deque[tuple[AudioHeader, bytes]] = deque()
        self.sample_rate: int | None = None
        self.epoch = 0
        self.connected = False
        self.intentional = False
        self.error = 'Waiting for recs audio'
        self.last_received = float('-inf')
        self.dropped_frames = 0
        self.clock_id: str | None = None

    def start(self) -> None:
        self.thread.start()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                inspection = self.connection.inspect()
                if inspection.paused or inspection.muted:
                    self.lost(
                        'recs recording is paused'
                        if inspection.paused
                        else 'recs microphone is muted',
                        intentional=True,
                    )
                elif not inspection.enabled:
                    self.lost('Audio forwarding is disabled in recs', intentional=False)
                elif inspection.state != 'idle':
                    self.lost(
                        'recs source is unavailable or already subscribed',
                        intentional=False,
                    )
                elif not self.stopped.is_set():
                    self.receive(inspection)
            except (OSError, EOFError, ValueError, MemoryError) as error:
                self.lost(
                    f'recs input unavailable ({type(error).__name__})',
                    intentional=False,
                )
            self.stopped.wait(RETRY_SECONDS)

    def receive(self, inspection: RecsInspection) -> None:
        response = self.connection.subscribe(inspection)
        client = None
        try:
            client = AudioClient(response.subscription, timeout=0.5)
            with self.lock:
                if self.stopped.is_set():
                    return
                self.client = client
                self.received.clear()
                self.sample_rate = response.subscription.sample_rate
                self.clock_id = response.subscription.clock_id
                self.epoch += 1
                self.connected = True
                self.intentional = False
                self.last_received = float('-inf')
            end_frame: int | None = None
            while not self.stopped.is_set():
                header, pcm = client.receive()
                with self.lock:
                    if self.stopped.is_set():
                        break
                    if end_frame is not None and header.start_frame > end_frame:
                        self.dropped_frames += header.start_frame - end_frame
                    end_frame = header.start_frame + header.frames
                    self.accept_packet(header, pcm)
        finally:
            with self.lock:
                self.client = None
            self.lost('recs input disconnected', intentional=False)
            if client is not None:
                client.close()
            self.connection.cancel(response.subscription.subscription_id)

    def accept_packet(self, header: AudioHeader, pcm: bytes) -> None:
        """Called with the receiver lock held; keep at most 100 ms of packets."""
        assert self.sample_rate is not None
        if header.frames > self.sample_rate // 50:
            raise ValueError('recs packet exceeds the supported 20 ms block duration')
        while (
            sum(h.frames for h, _ in self.received) + header.frames
            > self.sample_rate // 10
        ):
            old, _ = self.received.popleft()
            self.dropped_frames += old.frames
        self.received.append((header, pcm))
        self.last_received = time.monotonic()
        self.error = (
            f'recs capture status: {header.input_status}' if header.input_status else ''
        )

    def lost(self, message: str, *, intentional: bool) -> None:
        with self.lock:
            self.dropped_frames += sum(h.frames for h, _ in self.received)
            self.received.clear()
            self.connected = False
            self.intentional = intentional
            self.error = message
            self.epoch += 1

    def close(self) -> None:
        self.stopped.set()
        with self.lock:
            client = self.client
        if client is not None:
            client.close()
        if self.thread.ident is not None:
            self.thread.join(2)
        self.lost('recs input stopped', intentional=False)


class AudioFormatChanged(RuntimeError):
    """An active encoder must be rebuilt before accepting the new native rate."""


class RecsInputLost(RuntimeError):
    pass


class RecsAudio(AudioOutput):
    def __init__(
        self,
        settings: RecsInput,
        sample_rate: int,
        output: IO[bytes] | None,
        state: RuntimeState,
        closing: ClosingSequence | None = None,
    ) -> None:
        super().__init__(sample_rate, output, state, closing)
        self.receiver = RecsReceiver(settings)
        self.seen_epoch = 0
        self.outage_since: float | None = time.monotonic()
        self.silence_due = time.monotonic()
        self.pending_since: float | None = None

    def attach_output(self, output: IO[bytes] | None) -> None:
        with self.receiver.lock:
            self.receiver.dropped_frames += sum(
                h.frames for h, _ in self.receiver.received
            )
            self.receiver.received.clear()
            super().attach_output(output)
            self.silence_due = time.monotonic()
            self.pending_since = None

    def update(self) -> None:
        now = time.monotonic()
        with self.receiver.lock:
            receiver = self.receiver
            if receiver.epoch != self.seen_epoch:
                self.discard_audio(align=True)
                self.pending_since = now
                self.seen_epoch = receiver.epoch
            if (
                self.pending
                and self.pending_since is not None
                and now - self.pending_since > MAX_AUDIO_AGE_SECONDS
            ):
                self.discard_audio(align=True)
                self.pending_since = now
            if (
                receiver.sample_rate is not None
                and receiver.sample_rate != self.sample_rate
            ):
                self.sample_rate = receiver.sample_rate
                self.discard_audio(align=False)
                if self.output is not None:
                    raise AudioFormatChanged('recs native sample rate changed')
            while (
                receiver.received
                and now - receiver.received[0][0].offered_at > MAX_AUDIO_AGE_SECONDS
            ):
                old, _ = receiver.received.popleft()
                receiver.dropped_frames += old.frames
            healthy = (
                receiver.connected
                and now - receiver.last_received <= MAX_AUDIO_AGE_SECONDS
            )
            if receiver.intentional or healthy:
                self.outage_since = None
            elif self.outage_since is None:
                self.outage_since = now
            if (
                self.outage_since is not None
                and now - self.outage_since >= OUTAGE_SECONDS
            ):
                self.error = 'recs input unavailable for 30 seconds; broadcast stopped'
                self.state.set_audio_health(
                    self.error, self.dropped_frames + receiver.dropped_frames
                )
                raise RecsInputLost(self.error)
            self.input_healthy = healthy
            self.state.set_audio_input(
                {
                    'kind': 'recs',
                    'source': receiver.connection.settings.source,
                    'channels': list(receiver.connection.settings.channels),
                    'connected': healthy,
                    'intentional_pause': receiver.intentional,
                    'sample_rate': self.sample_rate,
                    'clock_id': receiver.clock_id,
                }
            )
            self.error = receiver.error or (
                None if healthy else 'Waiting for recs audio'
            )
            if not self.pending:
                if receiver.received:
                    header = receiver.received[0][0]
                    status = next(
                        (
                            h.input_status
                            for h, _ in receiver.received
                            if h.input_status
                        ),
                        '',
                    )
                    pcm = b''.join(p for _, p in receiver.received)
                    receiver.received.clear()
                    block = np.frombuffer(pcm, dtype='<f4').reshape(-1, 2).copy()
                    self.blocks.put_nowait((block, status))
                    self.pending_since = header.offered_at
                    self.silence_due = (
                        max(now, self.silence_due) + len(block) / self.sample_rate
                    )
                elif self.output is not None and now >= self.silence_due:
                    frames = max(1, self.sample_rate // 100)
                    self.pending_since = now
                    self.blocks.put_nowait(
                        (np.zeros((frames, 2), dtype=np.float32), '')
                    )
                    self.silence_due = now + frames / self.sample_rate
            self.overflow_frames = receiver.dropped_frames
            self.write_audio()

    def close_capture(self) -> None:
        self.receiver.close()
        self.discard_audio(align=False)
