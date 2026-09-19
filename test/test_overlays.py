import io
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from pytest_regressions.image_regression import ImageRegressionFixture
from reccy.protocol import ipc, rpc

from streamo.config import Streamo
from streamo.control import ControlController
from streamo.overlays import LiveOverlays, SlateCue, TitleCue, write_overlay_frames
from streamo.runtime import RuntimeState
from test.test_config import _service


@pytest.fixture
def config(tmp_path: Path) -> Streamo:
    return Streamo(
        device_name='unused',
        channel=1,
        video=tmp_path / 'bed.mp4',
        streaming_service=_service(),
        video_resolution='160x90',
        image_dirs=[tmp_path],
        title_interval=3,
        title_duration=2,
        title_fade=1,
        video_frame_rate=4,
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
        update={'image_interval': 3, 'image_duration': 2, 'image_fade': 1}
    )
    overlays = LiveOverlays(config, set())
    overlays.frame()  # Start of fade, transparent.
    overlays.set_slate(SlateCue(visible=True))
    for _ in range(20):
        overlays.frame()
    overlays.set_slate(SlateCue(visible=False))
    frame, _ = overlays.frame()
    pixels = np.frombuffer(frame, dtype=np.uint8).reshape((360, 640, 4))
    assert pixels[180, 320, 3] == 64


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


def test_image_controls_report_applied_photo_and_survive_recovery(
    config: Streamo,
) -> None:
    path = config.primary_image_dir / 'photo.png'
    Image.new('RGBA', (160, 90), 'red').save(path)
    overlays = LiveOverlays(
        config.model_copy(
            update={'image_interval': 3, 'image_duration': 2, 'image_fade': 0}
        ),
        set(),
    )
    controller = ControlController(RuntimeState(), overlays=overlays)
    controller.state.set_muted(True)
    for command, params in (
        ('image_next', {'id': path.name}),
        ('image_pause', {'paused': True}),
    ):
        assert not isinstance(
            controller.handle_request(rpc.Request(command=command, params=params)),
            ipc.Error,
        )
    assert overlays.snapshot()['images'] == {'paused': True, 'next_id': path.name}
    overlays.begin_attempt()
    assert overlays.snapshot()['images'] == {'paused': True, 'next_id': path.name}
    controller.handle_request(
        rpc.Request(command='image_pause', params={'paused': False})
    )
    _, visual = overlays.frame()
    assert visual['image_id'] == path.name
    assert overlays.snapshot()['applied'] is None
    overlays.mark_applied(visual)
    assert overlays.snapshot()['applied']['image_id'] == path.name
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
