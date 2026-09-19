import enum
import threading
import time
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from .config import Streamo
from .images import ImageFrameProducer, ImageScheduler, load_image
from .moderation import ImageApproval


class TitleVisibility(enum.StrEnum):
    auto = enum.auto()
    show = enum.auto()
    hide = enum.auto()


class TitleCue(BaseModel, frozen=True):
    visibility: TitleVisibility = Field(strict=False)
    text: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra='forbid', strict=True)


class SlateCue(BaseModel, frozen=True):
    visible: bool
    text: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra='forbid', strict=True)


class ImagePause(BaseModel, frozen=True):
    paused: bool

    model_config = ConfigDict(extra='forbid', strict=True)


class ImageNext(BaseModel, frozen=True):
    id: str

    model_config = ConfigDict(extra='forbid', strict=True)


class LiveOverlays:
    def __init__(self, config: Streamo, initial_paths: set[Path]) -> None:
        encoding = config.streaming_service.encoding.video
        assert encoding is not None
        width, height = encoding.resolution.split('x')
        self.size = (int(width), int(height))
        width, height = config.video_resolution.split('x')
        self.working_size = (int(width), int(height))
        self.frame_rate = config.video_frame_rate
        self.title_interval = config.title_interval
        self.title_duration = config.title_duration
        self.title_fade = config.title_fade
        self.title = (
            Image.fromarray(load_image(config.title_card, *self.working_size))
            if config.title_card is not None
            else None
        )
        self.title_visibility = TitleVisibility.auto
        self.title_text: str | None = None
        self.slate_text = 'Intermission'
        self.slate = render_text('Intermission', self.working_size)
        self.slate_visible = False
        self.lock = threading.Lock()
        self.revision = 1
        self.applied: dict[str, object] | None = None
        self.frame_index = 0
        self.approval = ImageApproval(
            config.primary_image_dir, config.image_approval_required
        )
        self.photos = (
            ImageFrameProducer(
                ImageScheduler(
                    config.image_dir,
                    initial_paths=initial_paths,
                    session_weight=config.current_session_image_weight,
                    approval=self.approval,
                    directory_weights=config.resolved_image_dir_weights,
                ),
                width=self.working_size[0],
                height=self.working_size[1],
                frame_rate=config.video_frame_rate,
                interval=config.image_interval,
                duration=config.image_duration,
                fade=config.image_fade,
            )
            if config.image_interval > 0
            else None
        )

    def set_title(self, cue: TitleCue) -> dict[str, object]:
        replacement = (
            render_text(cue.text, self.working_size) if cue.text is not None else None
        )
        with self.lock:
            if replacement is not None:
                self.title = replacement
                self.title_text = cue.text
            if cue.visibility == TitleVisibility.show and self.title is None:
                raise ValueError('No title is configured; supply text')
            self.title_visibility = cue.visibility
            self.revision += 1
            return self.requested()

    def set_slate(self, cue: SlateCue) -> dict[str, object]:
        replacement = (
            render_text(cue.text, self.working_size) if cue.text is not None else None
        )
        with self.lock:
            if replacement is not None:
                self.slate = replacement
                assert cue.text is not None
                self.slate_text = cue.text
            self.slate_visible = cue.visible
            self.revision += 1
            return self.requested()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                'enabled': True,
                'requested': self.requested(),
                'applied': None if self.applied is None else dict(self.applied),
                'images': self.photos.snapshot() if self.photos is not None else None,
            }

    def control_images(
        self, command: str, payload: dict[str, object]
    ) -> dict[str, object]:
        with self.lock:
            if self.photos is None:
                raise ValueError('Image controls require a positive image_interval')
            if command == 'image_pause':
                self.photos.paused = ImagePause.model_validate(payload).paused
            elif command == 'image_next':
                cue = ImageNext.model_validate(payload)
                self.photos.select_next(self.approval.image_path(cue.id))
            elif command == 'image_skip':
                if payload:
                    raise ValueError('image_skip accepts no parameters')
                self.photos.skip()
            else:
                raise ValueError('Unknown image control')
            self.revision += 1
            return self.photos.snapshot()

    def requested(self) -> dict[str, object]:
        # Caller holds the lock, so a revision and its contents stay together.
        return {
            'revision': self.revision,
            'title_visibility': self.title_visibility.value,
            'title_text': self.title_text,
            'slate_text': self.slate_text,
            'slate_visible': self.slate_visible,
        }

    def begin_attempt(self) -> None:
        with self.lock:
            self.applied = None

    def frame(self) -> tuple[bytes, dict[str, object]]:
        with self.lock:
            canvas = Image.new(
                'RGBA', self.size, 'black' if self.slate_visible else (0, 0, 0, 0)
            )
            position = (
                (self.size[0] - self.working_size[0]) // 2,
                (self.size[1] - self.working_size[1]) // 2,
            )
            if self.slate_visible:
                canvas.alpha_composite(self.slate, position)
            else:
                elapsed = (self.frame_index / self.frame_rate) % self.title_interval
                opacity = (
                    1.0
                    if self.title_visibility == TitleVisibility.show
                    else 0.0
                    if self.title_visibility == TitleVisibility.hide
                    else title_opacity(elapsed, self.title_duration, self.title_fade)
                )
                if self.title is not None and opacity > 0:
                    title = self.title.copy()
                    title.putalpha(
                        title.getchannel('A').point(lambda a: round(a * opacity))
                    )
                    canvas.alpha_composite(title, position)
                if self.photos is not None:
                    photo = Image.frombytes(
                        'RGBA', self.working_size, self.photos.frame()
                    )
                    canvas.alpha_composite(photo, position)
                self.frame_index += 1
            return canvas.tobytes(), {
                **self.requested(),
                'image_id': self.photos.visible_id
                if self.photos is not None and not self.slate_visible
                else None,
            }

    def mark_applied(self, visual: dict[str, object]) -> None:
        with self.lock:
            self.applied = visual


def write_overlay_frames(stream: BinaryIO, producer: LiveOverlays) -> None:
    try:
        while True:
            started = time.monotonic()
            frame, visual = producer.frame()
            pending = memoryview(frame)
            while pending:
                written = stream.write(pending)
                if not written:
                    raise BrokenPipeError('Overlay pipe closed')
                pending = pending[written:]
            stream.flush()
            producer.mark_applied(visual)
            time.sleep(max(0, 1 / producer.frame_rate - (time.monotonic() - started)))
    except BrokenPipeError:
        pass
    finally:
        stream.close()


def title_opacity(elapsed: float, duration: float, fade: float) -> float:
    if elapsed >= duration:
        return 0.0
    if fade == 0:
        return 1.0
    return max(0.0, min(1.0, elapsed / fade, (duration - elapsed) / fade))


def render_text(text: str, size: tuple[int, int]) -> Image.Image:
    canvas = Image.new('RGBA', size)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=max(1, min(size[1] // 8, 48)))
    margin = max(1, size[0] // 20)
    lines: list[str] = []
    for paragraph in text.split('\n'):
        line = ''
        for character in paragraph:
            if draw.textlength(line + character, font=font) > size[0] - 2 * margin:
                if not line:
                    raise ValueError('Text does not fit the overlay resolution')
                lines.append(line)
                line = ''
            line += character
        lines.append(line)
    wrapped = '\n'.join(lines)
    box = draw.multiline_textbbox((0, 0), wrapped, font=font)
    if box[3] - box[1] > size[1] - 2 * margin:
        raise ValueError('Text does not fit the overlay resolution')
    position = (
        (size[0] - (box[2] - box[0])) // 2 - box[0],
        (size[1] - (box[3] - box[1])) // 2 - box[1],
    )
    draw.multiline_text(
        position,
        wrapped,
        font=font,
        fill='white',
        align='center',
        stroke_width=1,
        stroke_fill='black',
    )
    return canvas
