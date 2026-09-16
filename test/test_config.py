from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError
from reccy.reccy import Reccy

from streamo.config import Streamo
from streamo.images import ImageFeed
from streamo.provider_config import (
    AudioEncoding,
    EncodingProfile,
    RtmpIngest,
    TwitchService,
    VideoEncoding,
)


def _service() -> TwitchService:
    return TwitchService(
        service='twitch',
        ingest=RtmpIngest(
            protocol='rtmps',
            server_url='rtmps://live.twitch.tv/app',
            stream_key='key',
        ),
        encoding=EncodingProfile(
            container='flv',
            audio=AudioEncoding(
                codec='aac', bitrate='160k', sample_rate=48_000, channels=2
            ),
            video=VideoEncoding(
                codec='h264',
                bitrate='150k',
                resolution='640x360',
                frame_rate=10,
                keyframe_interval=2,
            ),
        ),
    )


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1.0])
def test_overlay_times_must_be_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('bed.mp4'),
            streaming_service=_service(),
            image_interval=value,
        )


def test_preview_does_not_initialize_provider(tmp_path: Path) -> None:
    video = tmp_path / 'bed.mp4'
    video.touch()
    config = Streamo(
        device_name='X18', channel=1, video=video, streaming_service=_service()
    )
    with (
        mock.patch(
            'streamo.config.adapter_for',
            side_effect=AssertionError('provider initialized'),
        ),
        mock.patch.object(Streamo, 'start'),
        mock.patch.object(Streamo, 'close'),
        mock.patch('streamo.streamer.stream', return_value=0),
    ):
        assert config.run(preview=True) == 0


def test_channel_must_be_positive() -> None:
    with pytest.raises(ValidationError, match='must be positive'):
        Streamo(
            device_name='X18',
            channel=0,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
        )


def test_streamo_requires_stereo_pair_start_channel() -> None:
    config = Streamo(
        device_name='X18',
        channel=17,
        video=Path('visual-bed.mp4'),
        streaming_service=_service(),
    )

    assert config.required_channels == 18
    assert config.streaming_service.service == 'twitch'
    assert config.local_display
    assert config.image_dir == Path('images')
    assert config.current_session_image_weight == 3
    assert isinstance(config, Reccy)


def test_local_display_can_be_disabled() -> None:
    config = Streamo(
        device_name='X18',
        channel=17,
        video=Path('visual-bed.mp4'),
        streaming_service=_service(),
        local_display=False,
    )

    assert not config.local_display


def test_title_card_must_exist() -> None:
    with pytest.raises(ValidationError, match='does not exist'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            title_card=Path('missing-title.png'),
        )


def test_title_duration_must_fit_interval(tmp_path: Path) -> None:
    title = tmp_path / 'title.png'
    title.touch()

    with pytest.raises(ValidationError, match='shorter than title_interval'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            title_card=title,
            title_interval=8,
            title_duration=8,
        )


def test_image_duration_must_fit_interval() -> None:
    with pytest.raises(ValidationError, match='shorter than image_interval'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            image_interval=8,
            image_duration=8,
        )


def test_image_feed_requires_enabled_participant_images() -> None:
    with pytest.raises(ValidationError, match='image_interval must be positive'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            image_feed=ImageFeed(
                url='https://ax.to/show/foto.php',
                token='room-secret-at-least-20-characters',
            ),
        )


def test_current_session_image_weight_must_not_be_negative() -> None:
    with pytest.raises(ValidationError, match='must not be negative'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            current_session_image_weight=-1,
        )


def test_image_feed_requires_http_url() -> None:
    with pytest.raises(ValidationError, match='must use HTTPS'):
        ImageFeed(
            url='http://ax.to/show/foto.php',
            token='room-secret-at-least-20-characters',
        )


def test_configuration_errors_do_not_expose_service_secrets() -> None:
    data = {
        'device_name': 'X18',
        'channel': 1,
        'streaming_service': {
            'service': 'unknown',
            'ingest': {
                'protocol': 'rtmps',
                'server_url': 'rtmps://ingest.example.test/app',
                'stream_key': 'must-not-leak',
            },
        },
    }

    with pytest.raises(ValidationError) as raised:
        Streamo.model_validate(data)

    assert 'must-not-leak' not in str(raised.value)
