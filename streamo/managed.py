import os
import time
from pathlib import Path

from pydantic import BaseModel, Field


class LaunchPermit(BaseModel, frozen=True):
    config_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expires_at: float = Field(allow_inf_nan=False)


class ManagedLaunchError(ValueError):
    pass


def launch_permit_path() -> Path:
    return Path('/run/user') / str(os.getuid()) / 'showco-twitch.json'


def read_launch_permit() -> LaunchPermit:
    import fcntl

    try:
        with launch_permit_path().open('rb') as file:
            try:
                fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                permit = LaunchPermit.model_validate_json(file.read())
                if time.monotonic() < permit.expires_at:
                    return permit
    except (OSError, ValueError):
        pass
    raise ManagedLaunchError('Managed publishing needs a current showCo authorization')
