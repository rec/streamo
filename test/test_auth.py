import base64
import hashlib
import stat
import tomllib
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from streamo import __main__, auth, kick_api


class FakeFlow:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def run_local_server(self, **options: object) -> SimpleNamespace:
        self.options = options
        return SimpleNamespace(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )


def test_authorize_youtube_writes_private_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    flow = FakeFlow()
    client_secrets = tmp_path / "client-secrets.json"
    credentials = tmp_path / "youtube-auth.toml"
    client_secrets.write_text("{}")
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        lambda path, scopes: flow,
    )

    auth.authorize_youtube(
        auth.AuthOptions(
            service="youtube",
            client_secrets=client_secrets,
            credentials=credentials,
            no_browser=True,
            callback_port=9876,
        )
    )

    assert tomllib.loads(credentials.read_text()) == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
    }
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert flow.options == {
        "host": "127.0.0.1",
        "port": 9876,
        "open_browser": False,
        "access_type": "offline",
        "prompt": "consent",
    }


def test_main_dispatches_auth(monkeypatch) -> None:
    received: list[str] = []
    monkeypatch.setattr(auth, "main", lambda arguments: received.extend(arguments) or 0)

    assert __main__.main(["auth", "youtube", "--no-browser"]) == 0
    assert received == ["youtube", "--no-browser"]


def test_authorize_kick_uses_pkce_and_writes_private_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    client_secrets = tmp_path / "kick-client.toml"
    credentials = tmp_path / "kick-auth.toml"
    client_secrets.write_text(
        'client_id = "client-id"\nclient_secret = "client-secret"\n'
    )
    received: dict[str, str] = {}

    def receive(url: str, state: str, *, callback_port: int, open_browser: bool) -> str:
        received.update(
            url=url,
            state=state,
            callback_port=str(callback_port),
            open_browser=str(open_browser),
        )
        return "authorization-code"

    def exchange(
        client: kick_api.KickClientSecrets,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> kick_api.KickToken:
        received.update(
            client_id=client.client_id,
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
        )
        return kick_api.KickToken(
            access_token=SecretStr("access-token"),
            refresh_token=SecretStr("refresh-token"),
            expires_in=3600,
        )

    monkeypatch.setattr(auth, "receive_kick_code", receive)
    monkeypatch.setattr(kick_api, "exchange_kick_code", exchange)

    auth.authorize_kick(
        auth.KickAuthOptions(
            client_secrets=client_secrets,
            credentials=credentials,
            no_browser=True,
            callback_port=9876,
        )
    )

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(received["url"]).query)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(received["verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query == {
        "response_type": ["code"],
        "client_id": ["client-id"],
        "redirect_uri": ["http://127.0.0.1:9876/"],
        "scope": [auth.KICK_SCOPES],
        "code_challenge": [challenge],
        "code_challenge_method": ["S256"],
        "state": [received["state"]],
    }
    assert received["code"] == "authorization-code"
    assert received["redirect_uri"] == "http://127.0.0.1:9876/"
    assert received["open_browser"] == "False"
    assert tomllib.loads(credentials.read_text()) == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
    }
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600


def test_auth_main_dispatches_kick(tmp_path: Path, monkeypatch) -> None:
    received: list[auth.KickAuthOptions] = []
    monkeypatch.setattr(auth, "authorize_kick", received.append)

    assert auth.main(["kick", "--client-secrets", str(tmp_path / "client.toml")]) == 0
    assert received[0].client_secrets == tmp_path / "client.toml"
