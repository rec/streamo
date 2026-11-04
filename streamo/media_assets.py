"""Finite local media snapshots held for the lifetime of external readers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from reccy.runtime.assets import AssetStore, SourceKind, source_fingerprint


@contextmanager
def prepare_video(path: Path) -> Iterator[Path]:
    """Give FFmpeg an immutable verified snapshot, then release it after shutdown."""
    source = path.expanduser().resolve()
    with TemporaryDirectory(prefix='streamo-video-') as directory:
        store = AssetStore(Path(directory), credential_scope='local-video')
        entry = store.import_file(
            source.parent,
            source.name,
            maximum_bytes=max(1, source.stat().st_size),
            source_key=source_fingerprint({'path': str(source)}, {}, None, {}),
            source_kind=SourceKind.local_file,
        )
        with store.verified_path(entry.id) as verified:
            yield verified
