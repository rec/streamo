import sys
from collections.abc import Callable

import pytest
from reccy.pytest_plugin import CliHelp

from scripts import auto_loop, loop_videos, preview_loops, reencode_videos, render
from streamo.__main__ import main


def test_help(cli_help: CliHelp) -> None:
    cli_help('streamo', main, subcommands=['auth', 'daemon', 'preflight'])


def test_authorization_help(cli_help: CliHelp) -> None:
    cli_help(
        'streamo auth',
        lambda: main(['auth', *sys.argv[1:]]),
        subcommands=['youtube', 'kick'],
    )


@pytest.mark.parametrize(
    'entrypoint',
    [
        auto_loop.main,
        preview_loops.main,
        loop_videos.main,
        reencode_videos.main,
        render.main,
    ],
    ids=['auto_loop', 'preview_loops', 'loop_videos', 'reencode_videos', 'render'],
)
def test_media_tool_help(cli_help: CliHelp, entrypoint: Callable[[], None]) -> None:
    def invoke() -> int:
        entrypoint()
        return 0

    cli_help(f'python -m {entrypoint.__module__}', invoke)
