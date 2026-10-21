import errno
import io
import socket
import urllib.error
from unittest import mock

import pytest

from streamo.network import (
    check_internet_connection,
    describe_twitch_connection_failure,
    twitch_ingest_failure,
)
from streamo.twitch_api import TwitchApiError, TwitchRequest, urllib_transport


def test_connectivity_uses_an_independent_destination_after_the_first_fails() -> None:
    with mock.patch(
        'streamo.network.socket.create_connection',
        side_effect=[TimeoutError(), mock.MagicMock()],
    ) as connect:
        assert 'Internet connectivity is available' in check_internet_connection()
    assert connect.call_count == 2
    assert connect.call_args_list[0].args != connect.call_args_list[1].args
    assert all(c.kwargs['timeout'] == 2 for c in connect.call_args_list)


def test_failed_probes_do_not_claim_that_the_local_network_is_down() -> None:
    with mock.patch('streamo.network.socket.create_connection', side_effect=OSError):
        result = check_internet_connection()
    assert 'No internet connectivity detected' in result
    assert 'local network may still be available' in result


@pytest.mark.parametrize(
    'error,expected',
    [
        (socket.gaierror('private-key'), 'Cannot resolve the Twitch hostname'),
        (TimeoutError('private-key'), 'Connection to Twitch timed out'),
        (
            OSError(errno.ENETUNREACH, 'private-key'),
            'Network route to Twitch is unavailable',
        ),
        (ConnectionRefusedError('private-key'), 'Connection to Twitch was refused'),
    ],
)
def test_connection_errors_preserve_the_reason_without_exposing_raw_details(
    error: OSError, expected: str
) -> None:
    with mock.patch(
        'streamo.network.socket.create_connection', return_value=mock.MagicMock()
    ):
        result = describe_twitch_connection_failure(error)
    assert result.startswith(expected)
    assert 'Internet connectivity is available' in result
    assert 'private-key' not in result


def test_encoder_network_failure_includes_connectivity_but_not_the_publish_url() -> (
    None
):
    with mock.patch(
        'streamo.network.socket.create_connection', side_effect=TimeoutError
    ):
        result = twitch_ingest_failure('Connection timed out: rtmp://host/private-key')
    assert result is not None
    assert 'Connection to Twitch timed out' in result
    assert 'No internet connectivity detected' in result
    assert 'private-key' not in result


def test_credential_rejection_omits_response_details_and_network_probes() -> None:
    error = urllib.error.HTTPError(
        'https://example.test', 401, 'private-key', {}, io.BytesIO(b'private-key')
    )
    with (
        mock.patch('urllib.request.urlopen', side_effect=error),
        mock.patch('streamo.network.socket.create_connection') as connect,
        pytest.raises(TwitchApiError, match='rejected the API credentials') as raised,
    ):
        urllib_transport(TwitchRequest('GET', 'https://example.test', {}, None))
    assert 'private-key' not in str(raised.value)
    connect.assert_not_called()


def test_api_timeout_is_reported_with_general_connectivity() -> None:
    with (
        mock.patch(
            'urllib.request.urlopen', side_effect=urllib.error.URLError(TimeoutError())
        ),
        mock.patch(
            'streamo.network.socket.create_connection', return_value=mock.MagicMock()
        ),
        pytest.raises(TwitchApiError, match='Internet connectivity is available'),
    ):
        urllib_transport(TwitchRequest('GET', 'https://example.test', {}, None))
