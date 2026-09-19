import tomllib
from pathlib import Path
from typing import Annotated, Literal

import tyro
from pydantic import BaseModel
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
        config = load_config(options.config)
        return config.run(preview=options.action == 'preview')
    if options.action == 'install':
        result = streamo.install_service(
            [
                'daemon',
                'run',
                '--config',
                str(options.config.expanduser().resolve()),
            ]
        )
    else:
        result = getattr(streamo, f'{options.action}_service')()
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
    image_dir = values.get('image_dir', ['images'])
    if isinstance(image_dir, list) and all(isinstance(d, str) for d in image_dir):
        values['image_dir'] = [resolve_path(path.parent, d) for d in image_dir]
    service = values.get('streaming_service')
    if isinstance(service, dict) and isinstance(service.get('credentials'), str):
        service['credentials'] = resolve_path(path.parent, service['credentials'])
    return Streamo.model_validate(values)


def resolve_path(base: Path, value: str) -> Path:
    return (base / Path(value).expanduser()).resolve()
