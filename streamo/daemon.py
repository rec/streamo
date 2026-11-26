import hashlib
import sys
import tomllib
from pathlib import Path
from typing import Annotated, Literal

import tyro
from pydantic import BaseModel, ValidationError
from reccy.services.controller import print_service_status

from .config import STREAMO_SERVICE, Streamo
from .managed import read_launch_permit
from .video_probe import VideoProbeError


class DaemonOptions(BaseModel, frozen=True):
    """Capture and publish a stereo pair with streamO.

    Use `streamo daemon ACTION` to manage the background service, or
    `streamo auth youtube --help` / `streamo auth kick --help` to authorize
    provider access. Without a command, streamO runs in the foreground.
    Use `streamo preflight --help` to check readiness without publishing.
    Use `streamo daemon validate` to validate configuration and movie cadence.
    """

    action: Annotated[
        Literal[
            'run',
            'preview',
            'validate',
            'install',
            'uninstall',
            'start',
            'stop',
            'restart',
            'status',
        ],
        tyro.conf.Positional,
    ] = 'run'
    config: Path = Path('~/.config/streamo/config.toml')
    # Install the Linux service without starting it or enabling automatic restarts.
    manual: bool = False
    managed: bool = False


def main(argv: list[str] | None = None) -> int:
    options = tyro.cli(DaemonOptions, args=argv)
    return run(options)


def run(options: DaemonOptions) -> int:
    if options.manual and options.action != 'install':
        sys.exit('--manual requires daemon install')
    if options.managed and options.action != 'run':
        sys.exit('--managed requires daemon run')
    streamo = Streamo.model_construct()
    if options.action in {'run', 'preview', 'validate'}:
        permit = None
        try:
            if options.managed:
                permit = read_launch_permit()
                config = load_config(
                    options.config, expected_sha256=permit.config_sha256
                )
                if config.recs_input is None or not config.recs_input.daemon:
                    sys.exit('Managed publishing requires audio from the recs daemon')
            else:
                config = load_config(options.config)
        except FileNotFoundError as error:
            sys.exit(f'streamO configuration file does not exist: {error.filename}')
        except ValidationError as error:
            path = options.config.expanduser().resolve()
            details = []
            for item in error.errors(include_input=False):
                location = item['loc']
                if 'twitch' in location and (
                    location[-1] == 'stream_key'
                    or item['msg'] == 'Value error, stream_key is required'
                ):
                    details.append(
                        "Twitch needs your account's stream key. Add "
                        '[streaming_service.ingest] with stream_key = "YOUR_KEY" '
                        'to this configuration file.'
                    )
                else:
                    details.append(
                        f'{".".join(str(p) for p in location)}: {item["msg"]}'
                    )
            sys.exit(f'Invalid streamO configuration {path}:\n' + '\n'.join(details))
        except (OSError, tomllib.TOMLDecodeError) as error:
            sys.exit(f'Cannot read streamO configuration {options.config}: {error}')
        except ValueError as error:
            sys.exit(str(error))
        if options.action == 'validate':
            try:
                config.validate_media(check_cadence=True)
            except (OSError, ValueError) as error:
                sys.exit(f'Invalid streamO configuration {options.config}: {error}')
            return 0
        try:
            return config.run(preview=options.action == 'preview', permit=permit)
        except VideoProbeError as error:
            sys.exit(f'Invalid streamO configuration {options.config}: {error}')
    if options.action == 'install':
        streamo.install_service(
            [
                'daemon',
                'run',
                '--config',
                str(options.config.expanduser().resolve()),
                *(['--managed'] if options.manual else []),
            ],
            manual=options.manual,
        )
    elif options.action != 'status':
        getattr(streamo, f'{options.action}_service')()
    result = streamo.service_status()
    print_service_status(STREAMO_SERVICE.name, result)
    if options.action == 'uninstall':
        return int(result.installed or result.running is True)
    if options.action == 'stop':
        return int(result.running is not False)
    if options.action == 'install':
        return int(
            not result.installed or (options.manual and result.running is not False)
        )
    return int(result.running is not True)


def load_config(path: Path, *, expected_sha256: str | None = None) -> Streamo:
    path = path.expanduser().resolve()
    content = path.read_bytes()
    if (
        expected_sha256 is not None
        and hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise ValueError('streamO configuration changed after readiness was checked')
    values = tomllib.loads(content.decode())
    for name in ('video', 'title_card'):
        if (value := values.get(name)) is not None and isinstance(value, str):
            values[name] = resolve_path(path.parent, value)
    closing = values.get('closing_credits')
    if isinstance(closing, dict) and isinstance(closing.get('pages'), list):
        for page in closing['pages']:
            if isinstance(page, dict) and isinstance(page.get('image'), str):
                page['image'] = resolve_path(path.parent, page['image'])
    image_dir = values.get('image_dir', ['images'])
    if isinstance(image_dir, list) and all(isinstance(d, str) for d in image_dir):
        values['image_dir'] = [resolve_path(path.parent, d) for d in image_dir]
    service = values.get('streaming_service')
    if isinstance(service, dict) and isinstance(service.get('credentials'), str):
        service['credentials'] = resolve_path(path.parent, service['credentials'])
    return Streamo.model_validate(values)


def resolve_path(base: Path, value: str) -> Path:
    return (base / Path(value).expanduser()).resolve()
