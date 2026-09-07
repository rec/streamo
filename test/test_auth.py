import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

from streamo import __main__, auth


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
