"""A durable closing operation driven by the program's wall clock."""

import json
import math
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from reccy.runtime.files import atomic_output


class CreditPage(BaseModel, frozen=True):
    text: str | None = None
    image: Path | None = None
    visible_seconds: float = Field(gt=0, allow_inf_nan=False)
    fade_in_seconds: float = Field(gt=0, allow_inf_nan=False)
    fade_out_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def one_source(self) -> 'CreditPage':
        if (self.text is None) == (self.image is None):
            raise ValueError('each credit page needs exactly one text or image')
        if self.text is not None and not self.text.strip():
            raise ValueError('credit page text cannot be empty')
        return self

    @property
    def duration(self) -> float:
        return self.fade_in_seconds + self.visible_seconds + self.fade_out_seconds

    model_config = ConfigDict(extra='forbid')


class ClosingCredits(BaseModel, frozen=True):
    pages: list[CreditPage] = Field(min_length=1)
    audio_fade_curve: Literal['linear', 'equal_power'] = 'linear'

    model_config = ConfigDict(extra='forbid')


DEFAULT_CREDITS = ClosingCredits(
    pages=[
        CreditPage(
            text='The humans made the music',
            visible_seconds=3,
            fade_in_seconds=1,
            fade_out_seconds=1,
        ),
        CreditPage(
            text='The machines merely helped',
            visible_seconds=3,
            fade_in_seconds=1,
            fade_out_seconds=1,
        ),
        CreditPage(
            text='Thank you for listening',
            visible_seconds=3,
            fade_in_seconds=1,
            fade_out_seconds=1,
        ),
    ]
)


class ClosingSequence:
    def __init__(self, config: ClosingCredits | None, state_path: Path) -> None:
        self.config = config
        self.state_path = state_path
        self.lock = threading.Lock()
        self.operation_id: str | None = None
        self.state = 'idle'
        self.started_at: float | None = None
        self.black_started_at: float | None = None
        self.error: str | None = None
        self._load()

    @property
    def page_end_seconds(self) -> float:
        if self.config is None:
            return 0.0
        return sum(p.duration for p in self.config.pages)

    @property
    def duration_seconds(self) -> float:
        return self.page_end_seconds + 4.0

    def start(
        self, operation_id: str, *, now: float | None = None
    ) -> dict[str, object]:
        if self.config is None:
            raise ValueError('closing_credits pages are not configured')
        if not operation_id or len(operation_id) > 100:
            raise ValueError('operation_id must contain 1 to 100 characters')
        with self.lock:
            if self.state == 'running':
                raise ValueError('a closing sequence is already running')
            self.operation_id = operation_id
            self.state = 'running'
            self.started_at = time.time() if now is None else now
            self.black_started_at = None
            self.error = None
            try:
                self._save()
            except OSError:
                self.state = 'failed'
                raise
            return self._snapshot(self.started_at)

    def snapshot(self, *, now: float | None = None) -> dict[str, object]:
        with self.lock:
            return self._snapshot(time.time() if now is None else now)

    def _snapshot(self, now: float) -> dict[str, object]:
        elapsed = (
            max(0.0, now - self.started_at) if self.started_at is not None else 0.0
        )
        page, _, phase = self.visual(elapsed)
        return {
            'operation_id': self.operation_id,
            'state': self.state,
            'phase': phase if self.state == 'running' else self.state,
            'page': page,
            'page_count': len(self.config.pages) if self.config is not None else 0,
            'elapsed_seconds': elapsed,
            'duration_seconds': self.duration_seconds,
            'started_at': self.started_at,
            'black_started_at': self.black_started_at,
            'black_at': self.started_at + self.page_end_seconds + 2
            if self.started_at is not None
            else None,
            'error': self.error,
        }

    def visual(self, elapsed: float) -> tuple[int | None, float, str]:
        if self.config is None:
            return None, 0.0, 'idle'
        position = 0.0
        for index, page in enumerate(self.config.pages, 1):
            if elapsed < position + page.duration:
                within = elapsed - position
                opacity = min(
                    1.0,
                    max(0.0, within / page.fade_in_seconds),
                    max(0.0, (page.duration - within) / page.fade_out_seconds),
                )
                return index, opacity, 'page'
            position += page.duration
        if elapsed < position + 2:
            return None, 0.0, 'video-fade'
        return None, 1.0, 'black'

    def video_black_opacity(self, elapsed: float) -> float:
        return max(0.0, min(1.0, (elapsed - self.page_end_seconds) / 2))

    def audio_gain(self, *, now: float | None = None) -> float:
        with self.lock:
            if (
                self.state != 'running'
                or self.started_at is None
                or self.config is None
            ):
                return 1.0
            elapsed = (time.time() if now is None else now) - self.started_at
            progress = max(0.0, min(1.0, elapsed / (self.page_end_seconds + 2)))
            if self.config.audio_fade_curve == 'equal_power':
                return math.cos(progress * math.pi / 2)
            return 1.0 - progress

    def tick(self, *, now: float | None = None) -> bool:
        with self.lock:
            if self.state != 'running' or self.started_at is None:
                return False
            current = time.time() if now is None else now
            if (
                self.black_started_at is None
                and current >= self.started_at + self.page_end_seconds + 2
            ):
                self.black_started_at = current
                self._save()
            return current >= self.started_at + self.duration_seconds

    def complete(self) -> None:
        with self.lock:
            if self.state == 'running':
                self.state = 'completed'
                self._save()

    def fail(self, error: str) -> None:
        with self.lock:
            if self.state == 'running':
                self.state = 'failed'
                self.error = error
                self._save()

    def _load(self) -> None:
        try:
            value = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return
        self.operation_id = value['operation_id']
        self.state = value['state']
        self.started_at = value['started_at']
        self.black_started_at = value['black_started_at']
        self.error = value['error']
        if self.state == 'running':
            self.state = 'failed'
            self.error = (
                'streamO restarted during closing credits; '
                'broadcast outcome is uncertain'
            )
            self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_output(self.state_path) as temporary:
            temporary.write_text(
                json.dumps(
                    {
                        'operation_id': self.operation_id,
                        'state': self.state,
                        'started_at': self.started_at,
                        'black_started_at': self.black_started_at,
                        'error': self.error,
                    }
                )
            )
