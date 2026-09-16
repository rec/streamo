import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

import sounddevice
import tyro
from PIL import Image
from pydantic import BaseModel, ValidationError, computed_field

from .composition import AUDIO_CODECS, VIDEO_CODECS
from .config import Streamo
from .daemon import load_config
from .providers import ServiceCapability, adapter_for


class Check(BaseModel, frozen=True):
    name: str
    status: Literal['pass', 'fail', 'warning', 'skipped']
    detail: str


class Report(BaseModel, frozen=True):
    checks: list[Check]

    @computed_field
    @property
    def ok(self) -> bool:
        return not any(c.status == 'fail' for c in self.checks)


class PreflightOptions(BaseModel, frozen=True):
    """Inspect local readiness and print JSON without publishing or starting a daemon.

    Passing checks do not prove delivery to viewers. Skipped checks remain untested.
    """

    config: Path = Path('~/.config/streamo/config.toml')
    probe_device: bool = False
    """Briefly open and close the audio input. May compete with a running capture."""
    probe_remote: bool = False
    """Query supported provider health. May refresh stored OAuth credentials.

    Does not prepare a stream or change provider metadata.
    """


def main(argv: list[str] | None = None) -> int:
    options = tyro.cli(PreflightOptions, args=argv)
    try:
        config = load_config(options.config)
    except (OSError, ValueError) as error:
        detail = f'Could not load configuration ({type(error).__name__})'
        if isinstance(error, ValidationError):
            fields = [str(e['loc']) for e in error.errors(include_input=False)]
            detail += ': ' + ', '.join(fields)
        report = Report(
            checks=[Check(name='configuration', status='fail', detail=detail)]
        )
    else:
        report = check(
            config, probe_device=options.probe_device, probe_remote=options.probe_remote
        )
    print(report.model_dump_json(indent=2))
    return int(not report.ok)


def check(
    config: Streamo, *, probe_device: bool = False, probe_remote: bool = False
) -> Report:
    checks = [
        Check(name='configuration', status='pass', detail='Configuration validated')
    ]
    checks.extend(check_audio(config, probe_device=probe_device))
    checks.extend(check_media(config))
    checks.append(check_encoders(config))
    checks.append(check_storage(config))
    checks.extend(check_display(config))
    checks.append(check_remote(config, probe_remote=probe_remote))
    return Report(checks=checks)


def check_audio(config: Streamo, *, probe_device: bool) -> list[Check]:
    try:
        device = sounddevice.query_devices(config.device_name, 'input')
    except (sounddevice.PortAudioError, ValueError, OSError) as error:
        return [
            Check(
                name='audio_device',
                status='fail',
                detail=(
                    f'Cannot select input {config.device_name!r} '
                    f'({type(error).__name__})'
                ),
            ),
            Check(
                name='audio_format', status='skipped', detail='No selected input device'
            ),
            Check(
                name='audio_open', status='skipped', detail='No selected input device'
            ),
        ]
    channels = int(device['max_input_channels'])
    detail = (
        f'{device["name"]}: {channels} inputs; '
        f'selected pair {config.channel}-{config.required_channels}'
    )
    if channels < config.required_channels:
        return [
            Check(
                name='audio_device',
                status='fail',
                detail=detail + '; selected pair exceeds available inputs',
            ),
            Check(
                name='audio_format',
                status='skipped',
                detail='Selected pair is unavailable',
            ),
            Check(
                name='audio_open',
                status='skipped',
                detail='Selected pair is unavailable',
            ),
        ]
    checks = [Check(name='audio_device', status='pass', detail=detail)]
    try:
        sounddevice.check_input_settings(
            device=config.device_name,
            channels=config.required_channels,
            samplerate=config.sample_rate,
            dtype='float32',
        )
    except (sounddevice.PortAudioError, ValueError, OSError):
        checks.append(
            Check(
                name='audio_format',
                status='fail',
                detail=(
                    f'Input does not support {config.sample_rate} Hz float32 '
                    f'capture with {config.required_channels} channels'
                ),
            )
        )
        checks.append(
            Check(
                name='audio_open', status='skipped', detail='Audio format check failed'
            )
        )
        return checks
    checks.append(
        Check(
            name='audio_format',
            status='pass',
            detail=(
                f'{config.sample_rate} Hz float32 capture supported; device not opened'
            ),
        )
    )
    if not probe_device:
        checks.append(
            Check(name='audio_open', status='skipped', detail='Requires --probe-device')
        )
    else:
        try:
            capture = sounddevice.InputStream(
                device=config.device_name,
                channels=config.required_channels,
                samplerate=config.sample_rate,
                dtype='float32',
                blocksize=1024,
            )
            capture.close()
        except (sounddevice.PortAudioError, ValueError, OSError) as error:
            checks.append(
                Check(
                    name='audio_open',
                    status='fail',
                    detail=f'Could not open and close input ({type(error).__name__})',
                )
            )
        else:
            checks.append(
                Check(
                    name='audio_open',
                    status='pass',
                    detail='Opened and closed input; signal quality not tested',
                )
            )
    return checks


def check_media(config: Streamo) -> list[Check]:
    checks: list[Check] = []
    if config.streaming_service.encoding.video is None:
        checks.append(Check(name='video', status='skipped', detail='Audio-only output'))
    else:
        assert config.video is not None
        try:
            result = subprocess.run(
                [
                    'ffprobe',
                    '-v',
                    'error',
                    '-select_streams',
                    'v:0',
                    '-show_entries',
                    'stream=codec_name,width,height,avg_frame_rate:format=duration',
                    '-of',
                    'json',
                    str(config.video),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            media = json.loads(result.stdout)
            streams = media.get('streams', [])
            if not streams:
                checks.append(
                    Check(
                        name='video',
                        status='fail',
                        detail='Visual bed has no video stream',
                    )
                )
            else:
                checks.append(
                    Check(name='video', status='pass', detail=json.dumps(media))
                )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            checks.append(
                Check(
                    name='video',
                    status='fail',
                    detail=f'Could not probe visual bed ({type(error).__name__})',
                )
            )
    if not config.live_overlays or config.streaming_service.encoding.video is None:
        checks.append(
            Check(name='title_card', status='skipped', detail='Overlays disabled')
        )
    elif config.title_card is None:
        checks.append(
            Check(
                name='title_card', status='skipped', detail='No title card configured'
            )
        )
    else:
        try:
            with Image.open(config.title_card) as image:
                image.load()
                detail = f'{image.format}: {image.width}x{image.height}'
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            checks.append(
                Check(
                    name='title_card',
                    status='fail',
                    detail=f'Could not decode title card ({type(error).__name__})',
                )
            )
        else:
            checks.append(Check(name='title_card', status='pass', detail=detail))
    return checks


def check_encoders(config: Streamo) -> Check:
    encoding = config.streaming_service.encoding
    required = [AUDIO_CODECS[encoding.audio.codec]]
    if encoding.video is not None:
        required.append(VIDEO_CODECS[encoding.video.codec])
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Check(
            name='encoders',
            status='fail',
            detail=f'Could not list FFmpeg encoders ({type(error).__name__})',
        )
    available = {p[1] for r in result.stdout.splitlines() if len(p := r.split()) >= 2}
    missing = [e for e in required if e not in available]
    return Check(
        name='encoders',
        status='fail' if missing else 'pass',
        detail='Missing: ' + ', '.join(missing)
        if missing
        else 'Available: ' + ', '.join(required),
    )


def check_storage(config: Streamo) -> Check:
    if config.image_interval == 0 or not config.live_overlays:
        return Check(
            name='image_storage', status='skipped', detail='Participant images disabled'
        )
    directory = config.image_dir
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    try:
        with tempfile.TemporaryFile(dir=directory):
            pass
    except OSError as error:
        return Check(
            name='image_storage',
            status='fail',
            detail=f'Cannot write image storage ({type(error).__name__})',
        )
    if directory != config.image_dir:
        return Check(
            name='image_storage',
            status='warning',
            detail=(
                f'{config.image_dir} does not exist; ancestor {directory} is writable'
            ),
        )
    return Check(name='image_storage', status='pass', detail=f'{directory} is writable')


def check_display(config: Streamo) -> list[Check]:
    if not config.local_display or config.streaming_service.encoding.video is None:
        return [
            Check(
                name='local_display',
                status='skipped',
                detail='Local display disabled or audio-only output',
            )
        ]
    checks = [
        Check(
            name='ffplay',
            status='pass' if shutil.which('ffplay') else 'fail',
            detail='FFplay executable lookup',
        )
    ]
    if sys.platform != 'linux':
        checks.append(
            Check(
                name='local_display',
                status='warning',
                detail=(
                    'Automatic HDMI display requires Linux DRM; '
                    'desktop preview is separate'
                ),
            )
        )
    else:
        cards = list(Path('/dev/dri').glob('card*'))
        accessible = any(os.access(p, os.R_OK | os.W_OK) for p in cards)
        checks.append(
            Check(
                name='local_display',
                status='warning' if not accessible else 'pass',
                detail='No accessible DRM card'
                if not accessible
                else 'Accessible DRM card found',
            )
        )
    checks.append(
        Check(
            name='display_playback',
            status='skipped',
            detail=(
                'SDL KMSDRM support, connector state, and playback '
                'require a target display test'
            ),
        )
    )
    return checks


def check_remote(config: Streamo, *, probe_remote: bool) -> Check:
    if not probe_remote:
        return Check(
            name='provider_access',
            status='skipped',
            detail='Requires --probe-remote; no provider contacted',
        )
    try:
        adapter = adapter_for(config.streaming_service)
        if ServiceCapability.HEALTH not in adapter.capabilities:
            return Check(
                name='provider_access',
                status='skipped',
                detail='No provider health probe available for this configuration',
            )
        health = adapter.health()
    except (ValueError, OSError) as error:
        return Check(
            name='provider_access',
            status='fail',
            detail=(
                f'Provider health probe failed ({type(error).__name__}); '
                'credential and API details omitted'
            ),
        )
    if health is None:
        return Check(
            name='provider_access',
            status='skipped',
            detail='No provider resource available to query',
        )
    return Check(
        name='provider_access',
        status='pass',
        detail=(
            'Provider health query succeeded; '
            'delivery and publish permissions are not verified'
        ),
    )
