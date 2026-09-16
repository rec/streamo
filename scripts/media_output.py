from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def new_media_output(output: Path) -> Iterator[Path]:
    """Publish a completed render without ever replacing an existing file."""
    if output.exists():
        raise FileExistsError(f'{output} already exists')
    with TemporaryDirectory(dir=output.parent, prefix='.streamo-render-') as directory:
        temporary = Path(directory) / output.name
        yield temporary
        output.hardlink_to(temporary)
