"""Bounded connectivity diagnostics used after a Twitch connection failure."""

import errno
import socket
import ssl


def check_internet_connection() -> str:
    # Numeric addresses keep DNS failure separate from general connectivity.
    for address in [('1.1.1.1', 443), ('8.8.8.8', 53)]:
        try:
            with socket.create_connection(address, timeout=2):
                return 'Internet connectivity is available'
        except OSError:
            continue
    return (
        'No internet connectivity detected: both independent connection checks failed; '
        'the local network may still be available'
    )


def describe_twitch_connection_failure(error: object) -> str:
    if isinstance(error, ssl.SSLCertVerificationError):
        return 'Twitch TLS certificate verification failed'
    if isinstance(error, ssl.SSLError):
        return 'Twitch TLS connection failed'
    if isinstance(error, socket.gaierror):
        reason = 'Cannot resolve the Twitch hostname'
    elif isinstance(error, TimeoutError):
        reason = 'Connection to Twitch timed out'
    elif isinstance(error, ConnectionRefusedError):
        reason = 'Connection to Twitch was refused'
    elif isinstance(error, ConnectionResetError):
        reason = 'Connection to Twitch was reset'
    elif isinstance(error, OSError) and error.errno in {
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }:
        reason = 'Network route to Twitch is unavailable'
    else:
        reason = 'Cannot connect to Twitch'
    return f'{reason}. {check_internet_connection()}'


def twitch_ingest_failure(diagnostics: str) -> str | None:
    """Classify known encoder failures without including credential-bearing text."""
    text = diagnostics.lower()
    if any(
        p in text
        for p in ['failed to resolve', 'name resolution', 'nodename nor servname']
    ):
        reason = 'Cannot resolve the Twitch hostname'
    elif 'network is unreachable' in text or 'no route to host' in text:
        reason = 'Network route to Twitch is unavailable'
    elif 'connection timed out' in text or 'connection timeout' in text:
        reason = 'Connection to Twitch timed out'
    elif 'connection refused' in text:
        reason = 'Connection to Twitch was refused'
    elif 'connection reset' in text:
        reason = 'Connection to Twitch was reset'
    elif (
        'certificate verify failed' in text or 'certificate verification failed' in text
    ):
        return 'Twitch TLS certificate verification failed'
    elif any(
        p in text
        for p in [
            'authentication failed',
            'invalid stream key',
            '401 unauthorized',
            '403 forbidden',
        ]
    ):
        return 'Twitch rejected the publishing credentials or permissions'
    elif 'cannot open connection' in text:
        reason = 'Cannot connect to Twitch'
    elif 'error opening output' in text:
        reason = 'Twitch output could not be opened'
    else:
        return None
    return f'{reason}. {check_internet_connection()}'
