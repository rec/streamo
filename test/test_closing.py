from pathlib import Path
from unittest import mock

import pytest
from PIL import Image
from reccy.protocol import ipc, rpc
from test_overlays import config as overlay_config

from streamo.closing import ClosingCredits, ClosingSequence, CreditPage
from streamo.control import ControlController
from streamo.overlays import LiveOverlays
from streamo.runtime import RuntimeState


def credits() -> ClosingCredits:
    return ClosingCredits(
        pages=[
            CreditPage(
                text='First credit',
                visible_seconds=2,
                fade_in_seconds=1,
                fade_out_seconds=1,
            ),
            CreditPage(
                text='Last credit',
                visible_seconds=3,
                fade_in_seconds=1,
                fade_out_seconds=1,
            ),
        ]
    )


def test_closing_boundaries_and_durable_black_interval(tmp_path: Path) -> None:
    path = tmp_path / 'closing.json'
    closing = ClosingSequence(credits(), path)
    started = closing.start('show-1', now=100)

    assert started['page'] == 1
    assert started['duration_seconds'] == 13
    assert started['black_at'] == 111
    assert closing.visual(0) == (1, 0.0, 'page')
    assert closing.visual(1) == (1, 1.0, 'page')
    assert closing.visual(3.5) == (1, 0.5, 'page')
    assert closing.visual(4) == (2, 0.0, 'page')
    assert closing.visual(9) == (None, 0.0, 'video-fade')
    assert closing.video_black_opacity(10) == 0.5
    assert closing.audio_gain(now=100) == 1
    assert closing.audio_gain(now=105.5) == 0.5
    assert closing.audio_gain(now=111) == 0
    assert not closing.tick(now=110.9)
    assert closing.tick(now=113)
    assert closing.snapshot(now=113)['black_started_at'] == 113

    closing.complete()
    assert closing.snapshot(now=113)['state'] == 'completed'
    assert ClosingSequence(credits(), path).snapshot(now=113)['state'] == 'completed'


def test_duplicate_start_and_restart(tmp_path: Path) -> None:
    path = tmp_path / 'closing.json'
    closing = ClosingSequence(credits(), path)
    closing.start('show-1', now=100)

    with pytest.raises(ValueError, match='already running'):
        closing.start('show-2', now=101)

    restarted = ClosingSequence(credits(), path)
    status = restarted.snapshot(now=101)
    assert status['operation_id'] == 'show-1'
    assert status['state'] == 'failed'
    assert 'restarted' in str(status['error'])


def test_credit_page_requires_one_readable_source() -> None:
    with pytest.raises(ValueError, match='exactly one'):
        CreditPage(visible_seconds=1, fade_in_seconds=1, fade_out_seconds=1)
    with pytest.raises(ValueError, match='greater than 0'):
        CreditPage(
            text='Hello', visible_seconds=0, fade_in_seconds=1, fade_out_seconds=1
        )


def test_start_control_rejects_duplicates_and_exposes_status(tmp_path: Path) -> None:
    state = RuntimeState()
    state.set_state('streaming')
    closing = ClosingSequence(credits(), tmp_path / 'closing.json')
    controller = ControlController(state, overlays=mock.Mock(), closing=closing)

    started = controller.handle_request(
        rpc.Request(command='close_start', params={'operation_id': 'show-1'})
    )
    assert isinstance(started, dict)
    assert started['operation_id'] == 'show-1'
    status = controller.handle_request(rpc.Request(command='status'))
    assert isinstance(status, dict)
    assert status['closing']['state'] == 'running'
    duplicate = controller.handle_request(
        rpc.Request(command='close_start', params={'operation_id': 'show-2'})
    )
    assert isinstance(duplicate, ipc.Error)


def test_credit_frames_fade_to_black(tmp_path: Path) -> None:
    config = overlay_config.__wrapped__(tmp_path)
    closing = ClosingSequence(
        config.resolved_closing_credits, tmp_path / 'closing.json'
    )
    closing.start('show-1', now=100)
    overlays = LiveOverlays(config, set(), closing)

    with mock.patch('streamo.closing.time.time', return_value=100):
        first, _ = overlays.frame()
    with mock.patch('streamo.closing.time.time', return_value=101):
        visible, _ = overlays.frame()
    with mock.patch('streamo.closing.time.time', return_value=117):
        black, _ = overlays.frame()

    size = overlays.size
    assert Image.frombytes('RGBA', size, first).getpixel((0, 0))[3] == 0
    assert Image.frombytes('RGBA', size, visible).getpixel((0, 0))[3] == 255
    assert Image.frombytes('RGBA', size, black).getpixel((0, 0)) == (0, 0, 0, 255)
