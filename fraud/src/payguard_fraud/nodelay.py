"""uvicorn h11 protocol that turns off Nagle's algorithm on every connection.

With ``--workers`` above 1, uvicorn binds the listening socket itself with
protocol number 0, and asyncio only sets ``TCP_NODELAY`` on accepted sockets
whose protocol is ``IPPROTO_TCP``. Worker connections therefore keep Nagle on.
uvicorn writes response headers and body separately, so on a keep-alive
connection the body waits for the client's delayed ACK: about 40 ms per request
on Linux. Use with ``--http payguard_fraud.nodelay:NoDelayH11Protocol``.
"""

from __future__ import annotations

import asyncio
import socket

from uvicorn.protocols.http.h11_impl import H11Protocol


def set_nodelay(sock: socket.socket | None) -> None:
    if sock is not None and sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class NoDelayH11Protocol(H11Protocol):
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        set_nodelay(transport.get_extra_info("socket"))
        super().connection_made(transport)
