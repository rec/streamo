import base64
import hashlib
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Annotated, Literal, cast

import tyro
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import BaseModel, field_validator

from .credentials import write_private_toml

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
KICK_AUTHORIZATION_URL = "https://id.kick.com/oauth/authorize"
KICK_SCOPES = "channel:read channel:write chat:write streamkey:read"


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


class KickAuthOptions(BaseModel, frozen=True):
    client_secrets: Path
    credentials: Path = Path.home() / ".config/streamo/kick-auth.toml"
    no_browser: bool = False
    callback_port: int = 8765

    @field_validator("callback_port")
    @classmethod
    def validate_callback_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("callback_port must be from 1 to 65535")
        return value


class KickCallbackServer(HTTPServer):
    expected_state: str
    code: str | None
    error: str | None


class KickCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(KickCallbackServer, self.server)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        state = first_query_value(query, "state")
        if state != server.expected_state:
            server.error = "Kick authorization returned an invalid state"
        elif error := first_query_value(query, "error"):
            server.error = f"Kick authorization failed: {error}"
        elif code := first_query_value(query, "code"):
            server.code = code
        else:
            server.error = "Kick authorization returned no code"
        message = (
            "Kick authorization complete. You can close this window."
            if server.error is None
            else server.error
        )
        body = message.encode()
        self.send_response(200 if server.error is None else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    if argv is not None and argv[:1] == ["kick"]:
        options = tyro.cli(KickAuthOptions, args=argv[1:])
        authorize_kick(options)
        return 0
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
    write_private_toml(
        options.credentials,
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
        },
    )
    print(f"Stored YouTube credentials in {options.credentials.expanduser()}")


def authorize_kick(options: KickAuthOptions) -> None:
    from .kick_api import KickClientSecrets, exchange_kick_code, load_toml_model

    client = load_toml_model(
        options.client_secrets, KickClientSecrets, "Kick client secrets"
    )
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    challenge_text = challenge.rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    redirect_uri = f"http://127.0.0.1:{options.callback_port}/"
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "scope": KICK_SCOPES,
            "code_challenge": challenge_text,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    authorization_url = f"{KICK_AUTHORIZATION_URL}?{query}"
    code = receive_kick_code(
        authorization_url,
        state,
        callback_port=options.callback_port,
        open_browser=not options.no_browser,
    )
    token = exchange_kick_code(client, code, verifier, redirect_uri)
    write_private_toml(
        options.credentials,
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret.get_secret_value(),
            "refresh_token": token.refresh_token.get_secret_value(),
        },
    )
    print(f"Stored Kick credentials in {options.credentials.expanduser()}")


def receive_kick_code(
    authorization_url: str,
    state: str,
    *,
    callback_port: int,
    open_browser: bool,
) -> str:
    server = KickCallbackServer(("127.0.0.1", callback_port), KickCallbackHandler)
    server.expected_state = state
    server.code = None
    server.error = None
    try:
        print(f"Open this URL to authorize Streamo:\n\n{authorization_url}\n")
        if open_browser:
            webbrowser.open(authorization_url)
        server.handle_request()
    finally:
        server.server_close()
    if server.error is not None:
        raise ValueError(server.error)
    if server.code is None:
        raise ValueError("Kick authorization returned no code")
    return server.code


def first_query_value(values: dict[str, list[str]], name: str) -> str | None:
    items = values.get(name)
    return items[0] if items else None
