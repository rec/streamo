from pathlib import Path

import pytest
from reccy.protocol import ipc, rpc

import streamo.control
from streamo.config import Streamo
from streamo.control import (
    ControlController,
    RuntimeState,
)
from streamo.services import (
    AudioEncoding,
    CustomService,
    EncodingProfile,
    RtmpIngest,
    adapter_for,
)


def test_status_request_returns_runtime_snapshot() -> None:
    state = RuntimeState()
    state.set_state("streaming")
    state.set_ffmpeg(alive=True)
    controller = ControlController(state=state)

    response = controller.handle_request(rpc.Request(command="status"))

    assert response == state.snapshot()


def test_mute_and_unmute_requests_change_runtime_state() -> None:
    state = RuntimeState()
    state.set_state("streaming")
    controller = ControlController(state=state)

    assert controller.handle_request(rpc.Request(command="mute")) == "ok"
    assert state.snapshot()["muted"] is True
    assert controller.handle_request(rpc.Request(command="unmute")) == "ok"
    assert state.snapshot()["muted"] is False


def test_stop_request_is_queued() -> None:
    controller = ControlController(state=RuntimeState())

    response = controller.handle_request(rpc.Request(command="stop"))

    assert response == "ok"
    assert controller.commands.get_nowait().name == "stop"


def test_unknown_request_returns_rpc_error() -> None:
    controller = ControlController(state=RuntimeState())

    response = controller.handle_request(rpc.Request(command="missing"))

    assert response == ipc.Error(type="error", message="unknown command missing")


def test_service_status_and_unsupported_capability_are_generic() -> None:
    state = RuntimeState()
    service = CustomService(
        service="custom",
        ingest=RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://ingest.example.test/app",
            stream_key="secret",
        ),
        encoding=EncodingProfile(
            container="flv",
            audio=AudioEncoding(
                codec="aac", bitrate="160k", sample_rate=48_000, channels=2
            ),
        ),
    )
    adapter = adapter_for(service)
    state.configure_service(adapter)
    controller = ControlController(state=state, service=adapter)

    status = controller.handle_request(rpc.Request(command="status"))
    response = controller.handle_request(rpc.Request(command="chat"))

    assert isinstance(status, dict)
    assert status["service"] == "custom"
    assert status["endpoint_host"] == "ingest.example.test"
    assert status["capabilities"] == ["publish", "stop"]
    assert response == ipc.Error(type="error", message="Custom does not support chat")


def test_image_request_copies_file_url_to_image_dir(tmp_path: Path) -> None:
    image_dir = tmp_path / "images"
    source = tmp_path / "source.png"
    source.write_bytes(b"first")
    (image_dir / "source.png").parent.mkdir(parents=True)
    (image_dir / "source.png").write_bytes(b"existing")
    controller = ControlController(state=RuntimeState(), image_dir=image_dir)

    response = controller.handle_request(
        rpc.Request(command="image", params={"urls": [source.as_uri()]})
    )

    target = image_dir / "source-2.png"
    assert response == {"images": [target.as_posix()]}
    assert target.read_bytes() == b"first"
    assert not any(p.suffix == ".part" for p in image_dir.iterdir())


def test_image_request_downloads_http_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_dir = tmp_path / "images"
    controller = ControlController(state=RuntimeState(), image_dir=image_dir)

    def urlopen(url: str, timeout: int) -> FakeHttpResponse:
        assert url == "https://example.test/card.png"
        assert timeout == 10
        return FakeHttpResponse(b"downloaded")

    monkeypatch.setattr(streamo.control, "urlopen", urlopen)

    response = controller.handle_request(
        rpc.Request(
            command="image",
            params={"urls": ["https://example.test/card.png"]},
        )
    )

    target = image_dir / "card.png"
    assert response == {"images": [target.as_posix()]}
    assert target.read_bytes() == b"downloaded"


def test_image_request_rejects_unsupported_url() -> None:
    controller = ControlController(state=RuntimeState())

    response = controller.handle_request(
        rpc.Request(command="image", params={"urls": ["ftp://example.test/card.png"]})
    )

    assert response == ipc.Error(
        type="error", message="unsupported image URL ftp://example.test/card.png"
    )


def test_daemon_uses_standard_reccy_control_path() -> None:
    streamo = Streamo.model_construct(home=Path("/tmp/streamo-home"))

    assert streamo.control_endpoint == Path(
        "/tmp/streamo-home/.local/state/streamo/gui.sock"
    )


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body
