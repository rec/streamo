import json
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import tyro
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import BaseModel, field_validator

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


class AuthOptions(BaseModel, frozen=True):
    service: Annotated[Literal["youtube"], tyro.conf.Positional]
    client_secrets: Path
    credentials: Path = Path.home() / ".config/streamo/youtube-auth.toml"
    no_browser: bool = False
    callback_port: int = 8765

    @field_validator("callback_port")
    @classmethod
    def validate_callback_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("callback_port must be from 1 to 65535")
        return value


def main(argv: list[str] | None = None) -> int:
    options = tyro.cli(AuthOptions, args=argv)
    authorize_youtube(options)
    return 0


def authorize_youtube(options: AuthOptions) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(
        options.client_secrets, scopes=[YOUTUBE_SCOPE]
    )
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=options.callback_port,
        open_browser=not options.no_browser,
        access_type="offline",
        prompt="consent",
    )
    if (
        credentials.client_id is None
        or credentials.client_secret is None
        or credentials.refresh_token is None
    ):
        raise ValueError("Google did not return reusable YouTube credentials")
    write_credentials(
        options.credentials,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        refresh_token=credentials.refresh_token,
    )
    print(f"Stored YouTube credentials in {options.credentials.expanduser()}")


def write_credentials(
    path: Path, *, client_id: str, client_secret: str, refresh_token: str
) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        f"client_id = {json.dumps(client_id)}\n"
        f"client_secret = {json.dumps(client_secret)}\n"
        f"refresh_token = {json.dumps(refresh_token)}\n"
    )
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
