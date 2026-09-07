from __future__ import annotations

import sys

from . import daemon


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["daemon"]:
        arguments = arguments[1:]
    return daemon.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
