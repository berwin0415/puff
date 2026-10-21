"""Explicit failure windows; consumption counts live in the SQLite transaction."""

import socket


FAULTS = frozenset({"none", "fail_once", "drop_receipt_once"})


def validate_fault(value: str) -> str:
    if value not in FAULTS:
        raise ValueError("Unknown fixture fault")
    return value


def drop_committed_response(handler) -> None:
    """Close after the caller committed its write, without sending an HTTP reply."""
    handler.close_connection = True
    try:
        handler.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    handler.connection.close()
