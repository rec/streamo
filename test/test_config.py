from pathlib import Path

import pytest
from pydantic import ValidationError
from reccy.reccy import Reccy

from streamo.config import Streamo
from streamo.services import (
    AudioEncoding,
    EncodingProfile,
    RtmpIngest,
    TwitchService,
    VideoEncoding,
)


def _service() -> TwitchService:
    return TwitchService(
        service="twitch",
        ingest=RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://live.twitch.tv/app",
            stream_key="key",
        ),
        encoding=EncodingProfile(
            container="flv",
            audio=AudioEncoding(
                codec="aac", bitrate="160k", sample_rate=48_000, channels=2
            ),
            video=VideoEncoding(
                codec="h264",
                bitrate="150k",
                resolution="640x360",
                frame_rate=10,
                keyframe_interval=2,
            ),
        ),
    )


def test_channel_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="must be positive"):
        Streamo(
            device_name="X18",
            channel=0,
            video=Path("visual-bed.mp4"),
            streaming_service=_service(),
        )


def test_streamo_requires_stereo_pair_start_channel() -> None:
    config = Streamo(
        device_name="X18",
        channel=17,
        video=Path("visual-bed.mp4"),
        streaming_service=_service(),
    )

    assert config.required_channels == 18
    assert config.streaming_service.service == "twitch"
    assert config.image_dir == Path("images")
    assert isinstance(config, Reccy)


def test_title_card_must_exist() -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Streamo(
            device_name="X18",
            channel=1,
            video=Path("visual-bed.mp4"),
            streaming_service=_service(),
            title_card=Path("missing-title.png"),
        )


def test_title_duration_must_fit_interval(tmp_path: Path) -> None:
    title = tmp_path / "title.png"
    title.touch()

    with pytest.raises(ValidationError, match="shorter than title_interval"):
        Streamo(
            device_name="X18",
            channel=1,
            video=Path("visual-bed.mp4"),
            streaming_service=_service(),
            title_card=title,
            title_interval=8,
            title_duration=8,
        )


def test_image_duration_must_fit_interval() -> None:
    with pytest.raises(ValidationError, match="shorter than image_interval"):
        Streamo(
            device_name="X18",
            channel=1,
            video=Path("visual-bed.mp4"),
            streaming_service=_service(),
            image_interval=8,
            image_duration=8,
        )


def test_configuration_errors_do_not_expose_service_secrets() -> None:
    data = {
        "device_name": "X18",
        "channel": 1,
        "streaming_service": {
            "service": "unknown",
            "ingest": {
                "protocol": "rtmps",
                "server_url": "rtmps://ingest.example.test/app",
                "stream_key": "must-not-leak",
            },
        },
    }

    with pytest.raises(ValidationError) as raised:
        Streamo.model_validate(data)

    assert "must-not-leak" not in str(raised.value)
