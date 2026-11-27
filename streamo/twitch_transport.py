"""Monitor the encoder's Twitch socket independently of FFmpeg progress."""

from pathlib import Path
from urllib.parse import urlsplit

POLL_SECONDS = 1.0
STARTUP_SECONDS = 45.0
DISCONNECT_SECONDS = 5.0


class TwitchTransport:
    def __init__(
        self, pid: int, endpoint: str, now: float, proc: Path = Path('/proc')
    ) -> None:
        self.process = proc / str(pid)
        url = urlsplit(endpoint)
        port = url.port or (443 if url.scheme == 'rtmps' else 1935)
        self.port = f'{port:04X}'
        self.probe_after = now
        self.deadline = now + STARTUP_SECONDS
        self.connected = False

    def failure(self, now: float) -> str | None:
        if now < self.probe_after:
            return None
        self.probe_after = now + POLL_SECONDS
        if self._connected():
            self.connected = True
            self.deadline = now + DISCONNECT_SECONDS
        elif now >= self.deadline:
            return (
                'Twitch ingest connection dropped'
                if self.connected
                else 'Twitch ingest connection did not open'
            )
        return None

    def _connected(self) -> bool:
        descriptors = self.process / 'fd'
        try:
            entries = list(descriptors.iterdir())
        except FileNotFoundError:
            return False
        sockets: set[str] = set()
        for entry in entries:
            try:
                target = str(entry.readlink())
            except FileNotFoundError:
                continue
            if target.startswith('socket:['):
                sockets.add(target[8:-1])
        if not sockets:
            return False
        for table in ('tcp', 'tcp6'):
            try:
                with (self.process / 'net' / table).open() as file:
                    next(file, None)
                    for line in file:
                        fields = line.split()
                        if (
                            fields[3] == '01'
                            and fields[9] in sockets
                            and fields[2].endswith(':' + self.port)
                        ):
                            return True
            except FileNotFoundError:
                continue
        return False
