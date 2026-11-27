import os
from pathlib import Path

import pytest

from streamo.twitch_transport import TwitchTransport


@pytest.mark.skipif(os.name == 'nt', reason='Linux procfs fixture uses symlinks')
@pytest.mark.parametrize('table', ['tcp', 'tcp6'])
def test_lost_ingest_socket_is_detected_while_preview_socket_remains(
    tmp_path: Path, table: str
) -> None:
    process = tmp_path / '123'
    descriptors = process / 'fd'
    descriptors.mkdir(parents=True)
    (descriptors / '7').symlink_to('socket:[7]')
    (descriptors / '9').symlink_to('socket:[9]')
    net = process / 'net'
    net.mkdir()
    connections = net / table
    header = 'sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n'
    ingest = '0: 00000000:CAFE 00000000:01BB 01 0 0 0 0 0 7\n'
    other_process = '1: 00000000:CAFF 00000000:01BB 01 0 0 0 0 0 12\n'
    connections.write_text(header + ingest + other_process)
    transport = TwitchTransport(123, 'rtmps://ingest.example/app/key', 0, tmp_path)
    assert transport.failure(0) is None

    connections.write_text(header + other_process)
    assert transport.failure(1) is None
    assert transport.failure(4) is None
    assert transport.failure(5) == 'Twitch ingest connection dropped'


def test_no_initial_connection_reports_startup_failure(tmp_path: Path) -> None:
    transport = TwitchTransport(123, 'rtmp://ingest.example:9999/app/key', 0, tmp_path)
    assert transport.failure(0) is None
    assert transport.failure(44) is None
    assert transport.failure(45) == 'Twitch ingest connection did not open'
