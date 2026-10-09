from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError
from reccy.reccy import Reccy

from streamo.closing import CreditPage
from streamo.config import Streamo
from streamo.image_feed import ImageFeed
from streamo.provider_config import (
    AudioEncoding,
    EncodingProfile,
    RtmpIngest,
    TwitchService,
    VideoEncoding,
)
from streamo.runtime import HealthWarnings


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


def test_unit_bearing_stream_settings_normalize_to_runtime_numbers() -> None:
    service = _service().model_dump()
    service['encoding']['audio']['bitrate'] = '160kbps'
    service['encoding']['audio']['sample_rate'] = '48kHz'
    service['encoding']['video']['bitrate'] = '2.5Mbps'
    service['encoding']['video']['frame_rate'] = '30fps'
    service['encoding']['video']['keyframe_interval'] = '1500ms'
    config = Streamo.model_validate(
        {
            'device_name': 'X18',
            'channel': 1,
            'video': 'bed.mp4',
            'streaming_service': service,
            'sample_rate': '48kHz',
            'overlay_resolution': '1.28kpx x 720px',
            'overlay_frame_rate': '10fps',
            'title_interval': '3min',
            'title_duration': '8s',
            'title_fade': '500ms',
            'image_interval': '20s',
            'image_duration': '8s',
            'image_fade': '500ms',
        }
    )

    assert config.sample_rate == 48_000
    assert config.overlay_resolution == '1280x720'
    assert config.overlay_frame_rate == 10
    assert config.title_interval == 180
    assert config.title_fade == 0.5
    assert config.image_interval == 20
    assert config.image_fade == 0.5
    assert config.streaming_service.encoding.audio.bitrate == 160_000
    assert config.streaming_service.encoding.audio.sample_rate == 48_000
    assert config.streaming_service.encoding.video.bitrate == 2_500_000
    assert config.streaming_service.encoding.video.frame_rate == 30
    assert config.streaming_service.encoding.video.keyframe_interval == 1.5


def test_video_resolution_accepts_pixel_units() -> None:
    video = _service().encoding.video
    assert video is not None
    configured = video.model_copy(update={'resolution': '1.28kpx x 720px'})
    parsed = VideoEncoding.model_validate(configured.model_dump())
    assert parsed.resolution == '1280x720'


def test_other_unit_bearing_settings_accept_converted_units() -> None:
    warnings = HealthWarnings.model_validate(
        {
            'silence_seconds': '500ms',
            'silence_level_db': '-60dB',
            'clipping_seconds': '2s',
            'output_stall_seconds': '0.5min',
        }
    )
    feed = ImageFeed.model_validate(
        {
            'url': 'https://example.test/feed',
            'token': 'x' * 20,
            'poll_interval': '750ms',
        }
    )
    page = CreditPage.model_validate(
        {
            'text': 'Credits',
            'visible_seconds': '3s',
            'fade_in_seconds': '500ms',
            'fade_out_seconds': '0.5s',
        }
    )

    assert warnings.silence_seconds == 0.5
    assert warnings.silence_level_db == -60
    assert warnings.output_stall_seconds == 30
    assert feed.poll_interval == 0.75
    assert page.duration == 4


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


def test_unknown_operator_config_keys_are_rejected() -> None:
    service = _service().model_dump()
    service['recover_publsh'] = True
    config = {
        'device_name': 'X18',
        'channel': 1,
        'video': 'visual-bed.mp4',
        'streaming_service': service,
    }
    with pytest.raises(ValidationError, match='recover_publsh'):
        Streamo.model_validate(config)
    service.pop('recover_publsh')
    service['ingest']['backup_server_url'] = 'rtmps://backup.example.test/live'
    with pytest.raises(ValidationError, match='backup_server_url'):
        Streamo.model_validate(config)


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('sample_rate', 192_001),
        ('overlay_frame_rate', 61),
        ('overlay_resolution', '3840x2160'),
    ],
)
def test_capture_and_overlay_resources_are_bounded(field: str, value: object) -> None:
    values: dict[str, object] = {
        'device_name': 'X18',
        'channel': 1,
        'video': 'visual-bed.mp4',
        'streaming_service': _service(),
        field: value,
    }
    with pytest.raises(ValidationError, match=field):
        Streamo.model_validate(values)


def test_encoded_video_resources_are_bounded() -> None:
    with pytest.raises(ValidationError, match='resolution'):
        VideoEncoding(
            codec='h264',
            bitrate='2500k',
            resolution='7680x4320',
            frame_rate=30,
            keyframe_interval=2,
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
    assert config.image_dir == [Path('images')]
    assert config.resolved_image_dir_weights == [1]
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


def test_disabling_overlays_skips_title_and_remote_feed(tmp_path: Path) -> None:
    video = tmp_path / 'bed.mp4'
    video.touch()
    config = Streamo(
        device_name='unused',
        channel=1,
        video=video,
        streaming_service=_service(),
        live_overlays=False,
        title_card=tmp_path / 'missing.png',
        image_interval=20,
        image_feed=ImageFeed(url='https://example.test/photos.php', token='a' * 20),
    )
    with (
        mock.patch.object(Streamo, 'start'),
        mock.patch.object(Streamo, 'close'),
        mock.patch('streamo.streamer.stream', return_value=0),
        mock.patch(
            'streamo.config.ImageFeedPoller', side_effect=AssertionError('feed started')
        ),
    ):
        assert config.run(preview=True) == 0


def test_image_directory_weights_default_to_descending_order() -> None:
    config = Streamo(
        device_name='X18',
        channel=1,
        video=Path('visual-bed.mp4'),
        streaming_service=_service(),
        image_dir=[Path('a'), Path('b'), Path('c'), Path('d')],
    )

    assert config.resolved_image_dir_weights == [4, 3, 2, 1]


def test_image_directory_weights_parse_comma_separated_values() -> None:
    config = Streamo(
        device_name='X18',
        channel=1,
        video=Path('visual-bed.mp4'),
        streaming_service=_service(),
        image_dir=[Path('a'), Path('b'), Path('c')],
        image_dir_weights='6, 2, 1',
    )

    assert config.resolved_image_dir_weights == [6, 2, 1]


def test_image_directory_weights_accept_integer_list() -> None:
    config = Streamo(
        device_name='X18',
        channel=1,
        video=Path('visual-bed.mp4'),
        streaming_service=_service(),
        image_dir=[Path('a'), Path('b'), Path('c')],
        image_dir_weights=[6, 2, 1],
    )

    assert config.resolved_image_dir_weights == [6, 2, 1]


@pytest.mark.parametrize('weights', ['1', [1]])
def test_image_directory_weights_repeat_the_final_value(
    weights: str | list[int],
) -> None:
    config = Streamo(
        device_name='X18',
        channel=1,
        video=Path('visual-bed.mp4'),
        streaming_service=_service(),
        image_dir=[Path('a'), Path('b'), Path('c'), Path('d')],
        image_dir_weights=weights,
    )

    assert config.resolved_image_dir_weights == [1, 1, 1, 1]


def test_image_directory_weights_must_not_exceed_directories() -> None:
    with pytest.raises(ValidationError, match='must not exceed image_dir'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            image_dir=[Path('a'), Path('b')],
            image_dir_weights='1,2,3',
        )


def test_image_directory_weights_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match='must contain at least one weight'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            image_dir_weights=[],
        )


@pytest.mark.parametrize('weights', ['', '1,,2', '1,two'])
def test_image_directory_weights_must_contain_comma_separated_integers(
    weights: str,
) -> None:
    with pytest.raises(ValidationError, match='comma-separated integers'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            image_dir_weights=weights,
        )


def test_image_directories_must_be_distinct() -> None:
    with pytest.raises(ValidationError, match='entries must be distinct'):
        Streamo(
            device_name='X18',
            channel=1,
            video=Path('visual-bed.mp4'),
            streaming_service=_service(),
            image_dir=[Path('images'), Path('images')],
        )


def test_incoming_inbox_cannot_be_configured_as_trusted_image_dir() -> None:
    with pytest.raises(ValidationError, match='incoming image inbox'):
        Streamo(
            device_name='X18',
            channel=1,
            streaming_service=_service(),
            image_dir=[Path('images'), Path('images/incoming')],
        )
