import io
import threading
from math import pi
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from pytest_regressions.image_regression import ImageRegressionFixture
from reccy.protocol import ipc, rpc
from test_config import _service

from streamo.config import Streamo
from streamo.control import ControlController
from streamo.image_transform import ImageTransform
from streamo.overlays import LiveOverlays, SlateCue, TitleCue, write_overlay_frames
from streamo.runtime import RuntimeState


@pytest.fixture
def config(tmp_path: Path) -> Streamo:
    return Streamo(
        device_name='unused',
        channel=1,
        video=tmp_path / 'bed.mp4',
        streaming_service=_service(),
        overlay_resolution='160x90',
        image_dir=[tmp_path],
        title_interval=3,
        title_duration=2,
        title_fade=1,
        overlay_frame_rate=4,
    )


def test_overlay_switch_is_enabled_by_default_and_frozen(config: Streamo) -> None:
    assert config.live_overlays is True
    with pytest.raises(ValidationError, match='frozen'):
        config.live_overlays = False
    controller = ControlController(RuntimeState())
    assert isinstance(
        controller.handle_request(
            rpc.Request(command='live_overlays', params={'enabled': True})
        ),
        ipc.Error,
    )
    assert isinstance(
        controller.handle_request(
            rpc.Request(command='slate', params={'visible': True})
        ),
        ipc.Error,
    )


def test_title_layout_and_opaque_full_size_slate(
    config: Streamo, image_regression: ImageRegressionFixture
) -> None:
    overlays = LiveOverlays(config, set())
    overlays.set_title(TitleCue(visibility='show', text='Live show'))
    title, _ = overlays.frame()
    overlays.set_slate(SlateCue(visible=True))
    slate, _ = overlays.frame()
    title_image = Image.frombytes('RGBA', overlays.size, title)
    slate_image = Image.frombytes('RGBA', overlays.size, slate)
    assert title_image.getpixel((0, 0))[3] == 0
    assert slate_image.getchannel('A').getextrema() == (255, 255)
    preview = Image.new('RGBA', (640, 720))
    preview.paste(title_image, (0, 0))
    preview.paste(slate_image, (0, 360))
    contents = io.BytesIO()
    preview.save(contents, format='PNG')
    image_regression.check(contents.getvalue())


def test_slate_pauses_title_and_photo_timing(config: Streamo) -> None:
    Image.new('RGBA', (160, 90), 'red').save(config.primary_image_dir / 'photo.png')
    config = config.model_copy(
        update={
            'image_interval': 3,
            'image_duration': 2,
            'image_fade': 1,
            'image_fade_out': 1,
        }
    )
    overlays = LiveOverlays(config, set())
    overlays.frame()  # Start of fade, transparent.
    overlays.set_slate(SlateCue(visible=True))
    for _ in range(20):
        overlays.frame()
    overlays.set_slate(SlateCue(visible=False))
    frame, _ = overlays.frame()
    pixels = np.frombuffer(frame, dtype=np.uint8).reshape((360, 640, 4))
    assert pixels[180, 320, 3] == 38


def test_photos_fill_screen_with_slow_translucent_fades(
    config: Streamo, image_regression: ImageRegressionFixture
) -> None:
    Image.new('RGBA', (32, 18), 'red').save(config.primary_image_dir / 'photo.png')
    overlays = LiveOverlays(config.model_copy(update={'image_interval': 30}), set())
    frames = [
        Image.frombytes('RGBA', overlays.size, overlays.frame()[0]) for _ in range(81)
    ]

    assert frames[0].getchannel('A').getextrema() == (0, 0)
    assert frames[24].getchannel('A').getextrema()[1] == 76
    assert all(f.getchannel('A').getextrema()[1] == 153 for f in frames[48:65])
    assert frames[72].getchannel('A').getextrema()[1] == 76
    assert frames[80].getchannel('A').getextrema() == (0, 0)
    assert frames[48].getbbox() == (32, 18, 608, 342)
    background = Image.new('RGBA', overlays.size, 'blue')
    background.alpha_composite(frames[48])
    contents = io.BytesIO()
    background.save(contents, format='PNG')
    image_regression.check(contents.getvalue())


def test_transform_sequence_renders_crops_and_respects_pause_and_skip(
    config: Streamo, image_regression: ImageRegressionFixture
) -> None:
    source = Image.new('RGBA', (320, 180), 'red')
    source.paste('blue', (160, 0, 320, 90))
    source.paste('yellow', (0, 90, 160, 180))
    source.paste('green', (160, 90, 320, 180))
    source.paste((0, 0, 0, 0), (145, 75, 175, 105))
    source.save(config.primary_image_dir / 'photo.png')
    configured = Streamo.model_validate(
        {
            **config.model_dump(),
            'image_interval': 4,
            'image_transforms': [
                ImageTransform(duration=0),
                ImageTransform(duration=1),
                ImageTransform(duration=1, offset=(40, -20), zoom=2, rotation=pi / 4),
                ImageTransform(duration=1, offset=(-20, 10), zoom=3, rotation=pi / 2),
            ],
        }
    )
    overlays = LiveOverlays(configured, set())
    frames: list[Image.Image] = []
    for i in range(13):
        frame, visual = overlays.frame()
        frames.append(Image.frombytes('RGBA', overlays.size, frame))
        if i == 6:
            overlays.control_images('image_pause', {'paused': True})
            for _ in range(3):
                assert overlays.frame()[0] == frame
            overlays.control_images('image_pause', {'paused': False})
        if i in {0, 12}:
            assert visual['image_id'] is None
        else:
            assert visual['image_id'] == 'trusted/1/photo.png'

    assert frames[0].getbbox() is None
    assert frames[2].getchannel('A').getextrema()[1] == 76
    assert frames[4].getchannel('A').getextrema()[1] == 153
    assert frames[10].getbbox() == (0, 0, *overlays.size)
    assert frames[12].getbbox() is None
    preview = Image.new('RGBA', (480, 180))
    for i, f in enumerate(frames[1:12:2]):
        preview.paste(f.resize((160, 90)), ((i % 3) * 160, (i // 3) * 90))
    contents = io.BytesIO()
    preview.save(contents, format='PNG')
    image_regression.check(contents.getvalue())

    # A new interval restarts the same sequence; skipping keeps the gap empty.
    for _ in range(4):
        overlays.frame()
    frame, _ = overlays.frame()
    assert Image.frombytes('RGBA', overlays.size, frame).getbbox() is not None
    overlays.control_images('image_skip', {})
    frame, visual = overlays.frame()
    assert Image.frombytes('RGBA', overlays.size, frame).getbbox() is None
    assert visual['image_id'] is None


def test_title_auto_fades_and_hides(config: Streamo, tmp_path: Path) -> None:
    title = tmp_path / 'title.png'
    Image.new('RGBA', (160, 90), 'red').save(title)
    overlays = LiveOverlays(config.model_copy(update={'title_card': title}), set())
    alpha = []
    for _ in range(12):
        frame, _ = overlays.frame()
        alpha.append(frame[(180 * 640 + 320) * 4 + 3])
    assert alpha == [0, 64, 128, 191, 255, 191, 128, 64, 0, 0, 0, 0]


def test_failed_cue_preserves_title_and_mute(config: Streamo) -> None:
    overlays = LiveOverlays(config, set())
    controller = ControlController(RuntimeState(), overlays=overlays)
    controller.state.set_muted(True)
    assert not isinstance(
        controller.handle_request(
            rpc.Request(command='title', params={'visibility': 'show', 'text': 'Hello'})
        ),
        ipc.Error,
    )
    before = overlays.frame()
    for params in (
        {'visibility': 'bad'},
        {'visibility': 'show', 'text': 'long\n' * 100},
    ):
        assert isinstance(
            controller.handle_request(rpc.Request(command='title', params=params)),
            ipc.Error,
        )
        assert overlays.frame() == before
    assert not isinstance(
        controller.handle_request(
            rpc.Request(command='slate', params={'visible': True})
        ),
        ipc.Error,
    )
    assert controller.state.snapshot()['muted'] is True


class PartialWriter(io.BytesIO):
    def __init__(self, overlays: LiveOverlays) -> None:
        super().__init__()
        self.overlays = overlays
        self.expected = overlays.size[0] * overlays.size[1] * 4
        self.complete = False

    def write(self, data: bytes) -> int:
        if self.tell() == self.expected:
            requested = self.overlays.snapshot()['requested']
            assert isinstance(requested, dict)
            self.complete = self.overlays.snapshot()['applied'] == {
                **requested,
                'image_id': None,
            }
            raise BrokenPipeError
        assert self.overlays.snapshot()['applied'] is None
        return super().write(data[:1031])


def test_applied_revision_requires_complete_write_and_resets_on_recovery(
    config: Streamo,
) -> None:
    overlays = LiveOverlays(config, set())
    overlays.set_slate(SlateCue(visible=True, text='Back soon'))
    writer = PartialWriter(overlays)
    with mock.patch('streamo.overlays.time.sleep'):
        write_overlay_frames(writer, overlays)
    assert writer.complete
    requested = overlays.snapshot()['requested']
    overlays.begin_attempt()
    assert overlays.snapshot()['applied'] is None
    assert overlays.snapshot()['requested'] == requested
    writer = PartialWriter(overlays)
    with mock.patch('streamo.overlays.time.sleep'):
        write_overlay_frames(writer, overlays)
    assert writer.complete


def test_overlay_frames_catch_up_after_a_blocked_write() -> None:
    now = [0.0]
    applied: list[float] = []
    producer = mock.Mock(frame_rate=4)
    producer.frame.side_effect = [(b'frame', {})] * 5 + [BrokenPipeError]
    producer.mark_applied.side_effect = lambda visual: applied.append(now[0])
    writer = mock.Mock()

    def write(data: memoryview) -> int:
        if not applied:
            now[0] += 0.75
        return len(data)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    writer.write.side_effect = write
    with (
        mock.patch('streamo.overlays.time.monotonic', lambda: now[0]),
        mock.patch('streamo.overlays.time.sleep', sleep),
    ):
        write_overlay_frames(writer, producer)
    assert applied == [0.75, 0.75, 0.75, 0.75, 1.0]
    writer.close.assert_called_once()


def test_image_controls_report_applied_photo_and_survive_recovery(
    config: Streamo,
) -> None:
    path = config.primary_image_dir / 'photo.png'
    Image.new('RGBA', (160, 90), 'red').save(path)
    overlays = LiveOverlays(
        config.model_copy(
            update={
                'image_interval': 3,
                'image_duration': 2,
                'image_fade': 0,
                'image_fade_out': 0,
            }
        ),
        set(),
    )
    controller = ControlController(RuntimeState(), overlays=overlays)
    controller.state.set_muted(True)
    for command, params in (
        ('image_next', {'id': f'trusted/1/{path.name}'}),
        ('image_pause', {'paused': True}),
    ):
        assert not isinstance(
            controller.handle_request(rpc.Request(command=command, params=params)),
            ipc.Error,
        )
    assert overlays.snapshot()['images'] == {
        'paused': True,
        'next_id': f'trusted/1/{path.name}',
    }
    overlays.begin_attempt()
    assert overlays.snapshot()['images'] == {
        'paused': True,
        'next_id': f'trusted/1/{path.name}',
    }
    controller.handle_request(
        rpc.Request(command='image_pause', params={'paused': False})
    )
    _, visual = overlays.frame()
    assert visual['image_id'] == f'trusted/1/{path.name}'
    assert overlays.snapshot()['applied'] is None
    overlays.mark_applied(visual)
    assert overlays.snapshot()['applied']['image_id'] == f'trusted/1/{path.name}'
    overlays.set_slate(SlateCue(visible=True))
    _, visual = overlays.frame()
    assert visual['image_id'] is None
    overlays.set_slate(SlateCue(visible=False))
    controller.handle_request(rpc.Request(command='image_skip'))
    frame, visual = overlays.frame()
    assert visual['image_id'] is None
    assert not any(frame)
    assert controller.state.snapshot()['muted'] is True
    assert overlays.approval.allows(path)


@pytest.mark.parametrize(
    'command,params',
    [
        ('image_pause', {'paused': 'yes'}),
        ('image_next', {'id': '../outside.png'}),
        ('image_skip', {'id': 'photo.png'}),
    ],
)
def test_invalid_image_controls_leave_state_unchanged(
    config: Streamo, command: str, params: dict[str, object]
) -> None:
    overlays = LiveOverlays(config.model_copy(update={'image_interval': 20}), set())
    controller = ControlController(RuntimeState(), overlays=overlays)
    before = overlays.snapshot()
    assert isinstance(
        controller.handle_request(rpc.Request(command=command, params=params)),
        ipc.Error,
    )
    assert overlays.snapshot() == before


def test_image_controls_require_enabled_rotation(config: Streamo) -> None:
    controller = ControlController(RuntimeState(), overlays=LiveOverlays(config, set()))
    assert isinstance(
        controller.handle_request(rpc.Request(command='image_skip')), ipc.Error
    )


def test_controls_respond_while_image_read_is_delayed(config: Streamo) -> None:
    overlays = LiveOverlays(config.model_copy(update={'image_interval': 20}), set())
    assert overlays.photos is not None
    entered = threading.Event()
    release = threading.Event()
    controlled = threading.Event()

    def delayed_frame() -> bytes:
        entered.set()
        assert release.wait(2)
        return overlays.photos.transparent

    def control() -> None:
        overlays.set_slate(SlateCue(visible=True))
        overlays.control_images('image_pause', {'paused': True})
        assert overlays.snapshot()['requested']['slate_visible'] is True
        controlled.set()

    with mock.patch.object(overlays.photos, 'frame', side_effect=delayed_frame):
        frame_thread = threading.Thread(target=overlays.frame)
        frame_thread.start()
        assert entered.wait(1)
        control_thread = threading.Thread(target=control)
        control_thread.start()
        responsive = controlled.wait(0.5)
        release.set()
        frame_thread.join(2)
        control_thread.join(2)
    assert responsive
    assert not frame_thread.is_alive()
    assert not control_thread.is_alive()


def test_missing_next_image_reports_error_after_frame(config: Streamo) -> None:
    overlays = LiveOverlays(config.model_copy(update={'image_interval': 20}), set())
    assert overlays.control_images('image_next', {'id': 'incoming/missing.png'}) == {
        'paused': False,
        'next_id': 'incoming/missing.png',
    }
    overlays.frame()
    images = overlays.snapshot()['images']
    assert isinstance(images, dict)
    assert images['next_id'] is None
    assert 'must exist and be approved' in images['error']
