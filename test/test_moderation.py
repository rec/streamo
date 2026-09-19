import base64
import io
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image
from pydantic import ValidationError
from pytest_regressions.data_regression import DataRegressionFixture
from reccy.protocol import ipc, rpc

from streamo.config import Streamo
from streamo.control import ControlController
from streamo.images import ImageScheduler
from streamo.moderation import ImageApproval, ImageQueue, ImageReview
from streamo.overlays import LiveOverlays
from streamo.runtime import RuntimeState
from test.test_config import _service


@pytest.fixture
def controller(tmp_path: Path) -> ControlController:
    config = Streamo(
        device_name='unused',
        channel=1,
        video=tmp_path / 'bed.mp4',
        streaming_service=_service(),
        image_dir=[tmp_path],
        image_approval_required=True,
        image_interval=20,
        image_duration=10,
        image_fade=0,
    )
    return ControlController(
        RuntimeState(), image_dir=tmp_path, overlays=LiveOverlays(config, set())
    )


def test_unreviewed_images_wait_and_rejection_hides_cached_photo(
    controller: ControlController,
) -> None:
    overlays = controller.overlays
    assert overlays is not None
    path = controller.image_dir / 'photo.png'
    Image.new('RGBA', (640, 360), 'red').save(path)
    frame, _ = overlays.frame()
    assert not any(frame)
    reply = controller.handle_request(
        rpc.Request(
            command='image_review', params={'id': path.name, 'decision': 'approved'}
        )
    )
    assert not isinstance(reply, ipc.Error)
    # Finish the empty interval and start the approved image.
    for _ in range(200):
        frame, _ = overlays.frame()
    assert frame[:4] == bytes((255, 0, 0, 255))
    reply = controller.handle_request(
        rpc.Request(
            command='image_review', params={'id': path.name, 'decision': 'rejected'}
        )
    )
    assert not isinstance(reply, ipc.Error)
    frame, _ = overlays.frame()
    assert not any(frame)
    assert path.exists()
    assert not ImageApproval(controller.image_dir, required=True).allows(path)


def test_existing_and_new_images_require_approval_and_decisions_survive_restart(
    tmp_path: Path,
) -> None:
    old = tmp_path / 'old.png'
    old.touch()
    approval = ImageApproval(tmp_path, required=True)
    scheduler = ImageScheduler(tmp_path, approval=approval)
    new = tmp_path / 'new.png'
    new.touch()
    assert scheduler.next_image() is None
    approval.review(ImageReview(id=old.name, decision='approved'))
    approval.review(ImageReview(id=new.name, decision='rejected'))
    assert scheduler.next_image() == old
    restored = ImageApproval(tmp_path, required=True)
    assert restored.allows(old)
    assert not restored.allows(new)
    trusted = ImageApproval(tmp_path, required=False)
    other = tmp_path / 'other.png'
    other.touch()
    assert trusted.allows(other)
    assert not trusted.allows(new)


def test_queue_paginates_and_preview_is_bounded(
    controller: ControlController, data_regression: DataRegressionFixture
) -> None:
    for name in ('a.png', 'b.png', 'c.png'):
        Image.new('RGB', (800, 600), 'blue').save(controller.image_dir / name)
    first = controller.handle_request(
        rpc.Request(command='image_queue', params={'limit': 2})
    )
    second = controller.handle_request(
        rpc.Request(command='image_queue', params={'after': 'b.png', 'limit': 2})
    )
    data_regression.check({'first': first, 'second': second})
    preview = controller.handle_request(
        rpc.Request(command='image_preview', params={'id': 'a.png'})
    )
    assert isinstance(preview, dict)
    data_url = preview['data_url']
    assert isinstance(data_url, str)
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(',')[1]))) as image:
        assert image.size == (320, 180)
        assert image.getpixel((160, 90)) == (0, 0, 255, 255)


def test_failed_persistence_does_not_approve_an_image(tmp_path: Path) -> None:
    path = tmp_path / 'photo.png'
    path.touch()
    approval = ImageApproval(tmp_path, required=True)
    with mock.patch(
        'streamo.moderation.atomic_output', side_effect=OSError('disk full')
    ):
        with pytest.raises(OSError, match='disk full'):
            approval.review(ImageReview(id=path.name, decision='approved'))
    assert not approval.allows(path)
    assert not ImageApproval(tmp_path, required=True).allows(path)


def test_invalid_saved_decisions_fail_instead_of_accepting_images(
    tmp_path: Path,
) -> None:
    (tmp_path / '.streamo-image-approval.json').write_text('broken')
    with pytest.raises(ValidationError):
        ImageApproval(tmp_path, required=False)


@pytest.mark.parametrize(
    'command,params',
    [
        ('image_review', {'id': '../outside.png', 'decision': 'approved'}),
        ('image_review', {'id': 'missing.png', 'decision': 'approved'}),
        ('image_review', {'id': 'photo.png', 'decision': 'unknown'}),
        ('image_preview', {'id': '../outside.png'}),
        ('image_queue', {'limit': 0}),
        ('image_queue', {'limit': 101}),
    ],
)
def test_review_commands_reject_invalid_requests(
    controller: ControlController, command: str, params: dict[str, object]
) -> None:
    response = controller.handle_request(rpc.Request(command=command, params=params))
    assert isinstance(response, ipc.Error)


def test_automatic_acceptance_is_default(tmp_path: Path) -> None:
    path = tmp_path / 'photo.png'
    path.touch()
    approval = ImageApproval(tmp_path, required=False)
    assert ImageScheduler(tmp_path, approval=approval).next_image() == path
    assert not approval.path.exists()
    assert approval.queue(ImageQueue())['approval_required'] is False
