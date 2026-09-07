from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import tyro
from pydantic import BaseModel
from reccy.services.controller import print_service_status

from .config import STREAMO_SERVICE, Streamo


class DaemonOptions(BaseModel, frozen=True):
    action: Annotated[
        Literal[
            "run",
            "preview",
            "install",
            "uninstall",
            "start",
            "stop",
            "restart",
            "status",
        ],
        tyro.conf.Positional,
    ] = "run"
    config: Path = Path.home() / ".config/streamo/config.json"


def main(argv: list[str] | None = None) -> int:
    options = tyro.cli(DaemonOptions, args=argv)
    return run(options)


def run(options: DaemonOptions) -> int:
    streamo = Streamo.model_construct()
    if options.action in {"run", "preview"}:
        config = Streamo.model_validate_json(options.config.expanduser().read_text())
        return config.run(preview=options.action == "preview")
    if options.action == "install":
        result = streamo.install_service(
            [
                "daemon",
                "run",
                "--config",
                str(options.config.expanduser().resolve()),
            ]
        )
    else:
        result = getattr(streamo, f"{options.action}_service")()
    print_service_status(STREAMO_SERVICE.name, result)
    return 0 if result.running is not False else 1
