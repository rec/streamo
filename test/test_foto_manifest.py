import shutil
import subprocess
from pathlib import Path

import pytest


def test_partial_manifest_append_restores_manifest_and_removes_photo(
    tmp_path: Path,
) -> None:
    if shutil.which('php') is None:
        pytest.skip('PHP CLI is unavailable')
    fixture = Path(__file__).with_name('foto_manifest_rollback.php')
    result = subprocess.run(
        ['php', str(fixture), str(tmp_path / 'uploaded.jpg')],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
