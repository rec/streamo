from pathlib import Path

import pytest
from pydantic import ValidationError
from reccy.reccy import Reccy

from twitcho.config import Twitcho


def test_channel_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="must be positive"):
        Twitcho(
            device_name="X18",
            channel=0,
            video=Path("visual-bed.mp4"),
            twitch_key="key",
        )


def test_twitcho_requires_stereo_pair_start_channel() -> None:
    config = Twitcho(
        device_name="X18",
        channel=17,
        video=Path("visual-bed.mp4"),
        twitch_key="key",
    )

    assert config.required_channels == 18
    assert config.rtmp_url == "rtmp://live.twitch.tv/app/key"
    assert config.image_dir == Path("images")
    assert isinstance(config, Reccy)


def test_title_card_must_exist() -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Twitcho(
            device_name="X18",
            channel=1,
            video=Path("visual-bed.mp4"),
            twitch_key="key",
            title_card=Path("missing-title.png"),
        )


def test_title_duration_must_fit_interval(tmp_path: Path) -> None:
    title = tmp_path / "title.png"
    title.touch()

    with pytest.raises(ValidationError, match="shorter than title_interval"):
        Twitcho(
            device_name="X18",
            channel=1,
            video=Path("visual-bed.mp4"),
            twitch_key="key",
            title_card=title,
            title_interval=8,
            title_duration=8,
        )


def test_image_duration_must_fit_interval() -> None:
    with pytest.raises(ValidationError, match="shorter than image_interval"):
        Twitcho(
            device_name="X18",
            channel=1,
            video=Path("visual-bed.mp4"),
            twitch_key="key",
            image_interval=8,
            image_duration=8,
        )
