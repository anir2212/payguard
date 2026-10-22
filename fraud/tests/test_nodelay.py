"""The multi-worker scorer must not leave Nagle's algorithm on (see nodelay.py)."""

from __future__ import annotations

import socket

from uvicorn.config import Config

from payguard_fraud.nodelay import NoDelayH11Protocol, set_nodelay


def test_accepted_socket_from_protocol_zero_listener_gets_nodelay() -> None:
    # The same construction uvicorn's bind_socket() uses for --workers > 1.
    with socket.socket(family=socket.AF_INET) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with socket.create_connection(listener.getsockname()) as client:
            accepted, _ = listener.accept()
            with accepted:
                assert accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 0
                set_nodelay(accepted)
                assert accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
                client.sendall(b"x")


def test_unix_and_missing_sockets_are_left_alone() -> None:
    set_nodelay(None)
    left, right = socket.socketpair()
    with left, right:
        set_nodelay(left)


def test_uvicorn_loads_the_protocol_from_its_import_path() -> None:
    config = Config("payguard_fraud.score_api:app",
                    http="payguard_fraud.nodelay:NoDelayH11Protocol")
    config.load()
    assert config.http_protocol_class is NoDelayH11Protocol
