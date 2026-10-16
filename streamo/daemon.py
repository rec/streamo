import sys
import tomllib
from pathlib import Path
from typing import Annotated, Literal

import tyro
from pydantic import BaseModel, ValidationError
from reccy.services.controller import print_service_status

from .config import STREAMO_SERVICE, Streamo


class DaemonOptions(BaseModel, frozen=True):
    """Capture and publish a stereo pair with streamO.

    Use `streamo daemon ACTION` to manage the background service, or
    `streamo auth youtube --help` / `streamo auth kick --help` to authorize
    provider access. Without a command, streamO runs in the foreground.
    Use `streamo preflight --help` to check readiness without publishing.
    """

    action: Annotated[
        Literal[
            'run',
            'preview',
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


def main(argv: list[str] | None = None) -> int:
    options = tyro.cli(DaemonOptions, args=argv)
    return run(options)


def run(options: DaemonOptions) -> int:
    streamo = Streamo.model_construct()
    if options.action in {'run', 'preview'}:
        try:
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
        return config.run(preview=options.action == 'preview')
    if options.action == 'install':
        streamo.install_service(
            [
                'daemon',
                'run',
                '--config',
                str(options.config.expanduser().resolve()),
            ]
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
        return int(not result.installed)
    return int(result.running is not True)


def load_config(path: Path) -> Streamo:
    path = path.expanduser().resolve()
    values = tomllib.loads(path.read_text())
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
