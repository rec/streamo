import json
import tempfile
from collections.abc import Mapping
from pathlib import Path


def write_private_toml(path: Path, values: Mapping[str, str]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items())
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(contents)
        temporary.replace(path)
        path.chmod(0o600)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
