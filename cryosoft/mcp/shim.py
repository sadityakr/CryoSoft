"""The in-repo stdio backend: newline-delimited JSON-RPC, stdlib only.

MCP's stdio transport is one JSON-RPC message per line on stdin and stdout,
UTF-8, no embedded newlines — a framing small enough that depending on a
package for it would make the tests depend on that package too. So this
module is the backend that always exists: it reads frames, hands each to
``McpAdapter``, writes back what the adapter returns, and writes out the
app's notifications as they arrive.

**One loop, two readable things.** The session's stdin and the **Gateway
server**'s socket are waited on together with ``selectors``, so a state
change reaches the session the moment the app emits it rather than the next
time the session happens to ask a question. Where one of the two cannot be
selected on, the loop wakes on a timer instead and delivers the same
notifications a little later; nothing else differs.

**Not everything can be selected on.** ``select`` is the only mechanism the
stdlib offers on every platform, and on Windows it takes sockets alone: the
gateway's named pipe is not selectable there, and neither is stdin, which a
client hands over as an anonymous pipe. So the wait is chosen per descriptor
rather than assumed — a descriptor is probed before it is trusted, and a
stdin that fails the probe is read by a small thread that does nothing but
move bytes onto a queue. The session itself is still answered by one thread,
in the order the frames arrived.

**stdout carries protocol and nothing else.** Every diagnostic goes to
stderr, which is where a client shows it. A stray ``print`` here would
corrupt the session's message stream, which is the reason the logging
standard's "never print" rule is not merely a preference in this file.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import selectors
import sys
import threading
from collections.abc import Mapping
from typing import Any, BinaryIO

from cryosoft.mcp.adapter import McpAdapter

logger = logging.getLogger(__name__)

__all__ = ["MAX_FRAME_BYTES", "serve"]

#: The largest single stdin frame this backend will accept, in bytes. The
#: peer is the process that launched this one, not a network, so the cap is
#: a guard against a runaway writer rather than against an attacker.
MAX_FRAME_BYTES = 8 << 20

#: How long a wakeup waits when one of the two readable things cannot be
#: selected on and the loop has to fall back to a timer.
_POLL_INTERVAL_S = 0.25

#: How much is read from stdin at once.
_CHUNK = 65536

PARSE_ERROR = -32700


def serve(
    adapter: McpAdapter,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    """Serve one MCP session over stdio until the client closes stdin.

    Args:
        adapter: The translation this loop frames for. It is expected to be
            open already (``McpAdapter.open()``), so the session's first
            request is answered without a connect in the middle of it.
        stdin: The byte stream to read frames from; defaults to the
            process's own.
        stdout: The byte stream to write frames to; defaults to the
            process's own.

    Raises:
        OSError: If stdin or stdout fails in a way that leaves no session to
            serve; a failure to write one message is logged and the loop
            continues.
    """
    source = stdin if stdin is not None else sys.stdin.buffer
    sink = stdout if stdout is not None else sys.stdout.buffer
    buffer = bytearray()
    feed = _open_feed(source, adapter.client.fileno())

    logger.info("MCP adapter serving over stdio")
    try:
        while True:
            chunk = feed.wait()
            if chunk is None:
                logger.info("MCP adapter: the client closed stdin")
                return
            buffer.extend(chunk)
            # Whatever the wakeup was, anything the app emitted since the
            # last one goes out before the next answer does.
            _flush_notifications(adapter, sink)
            if not _consume(adapter, buffer, sink):
                return
    finally:
        feed.close()


class _SelectorFeed:
    """Wait on stdin and, when it can be waited on too, the gateway.

    This is the path every POSIX deployment takes: one ``select`` covers both
    readable things, so a gateway event is written out the instant it arrives
    rather than at the next request boundary.
    """

    def __init__(self, source_fd: int, gateway_fd: int | None) -> None:
        """Register what this loop waits on.

        Args:
            source_fd: The descriptor stdin frames are read from. It has
                already been probed as selectable.
            gateway_fd: The gateway connection's descriptor when it is
                selectable, else ``None`` — which costs a timer wakeup and
                nothing else.
        """
        self._selector = selectors.DefaultSelector()
        self._selector.register(source_fd, selectors.EVENT_READ, "stdin")
        if gateway_fd is not None:
            self._selector.register(gateway_fd, selectors.EVENT_READ, "gateway")
        self._timeout = None if gateway_fd is not None else _POLL_INTERVAL_S

    def wait(self) -> bytes | None:
        """Block until something happens, then report what stdin carried.

        Returns:
            The bytes read from stdin; ``b""`` for a wakeup that carried none
            — the gateway became readable, or the timer fired; ``None`` when
            the client closed stdin.
        """
        chunk = b""
        for key, _ in self._selector.select(self._timeout):
            if key.data == "gateway":
                continue
            piece = os.read(key.fd, _CHUNK)
            if not piece:
                return None
            chunk += piece
        return chunk

    def close(self) -> None:
        """Release the selector."""
        self._selector.close()


class _ThreadFeed:
    """Read stdin on its own thread, for a handle no selector accepts.

    The thread does one thing: block on a read and hand the bytes over. It
    never touches the adapter, so frames are still parsed and answered on the
    serving thread, in the order they arrived. The loop collects what the
    thread delivers and gives up waiting every ``_POLL_INTERVAL_S`` so the
    app's notifications still go out between requests.
    """

    def __init__(self, source: BinaryIO) -> None:
        """Start reading *source* in the background.

        Args:
            source: The byte stream to read frames from.
        """
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._pump,
            args=(source,),
            name="cryosoft-mcp-stdin",
            daemon=True,
        )
        self._thread.start()

    def _pump(self, source: BinaryIO) -> None:
        """Move stdin onto the queue until it ends.

        Args:
            source: The byte stream to read frames from.
        """
        # ``read1`` returns as soon as anything is there; a buffered
        # ``read(n)`` would sit on a whole frame waiting for n bytes.
        read = getattr(source, "read1", source.read)
        try:
            while True:
                chunk = read(_CHUNK)
                if not chunk:
                    return
                self._chunks.put(chunk)
        except (OSError, ValueError):
            logger.exception("MCP adapter could not read stdin")
        finally:
            self._chunks.put(None)

    def wait(self) -> bytes | None:
        """Take the next chunk the reader delivered.

        Returns:
            The bytes read from stdin; ``b""`` when the poll interval passed
            with nothing on the queue; ``None`` when the client closed stdin.
        """
        try:
            return self._chunks.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            return b""

    def close(self) -> None:
        """Let the reader go. It is a daemon and holds nothing to release."""


def _open_feed(source: BinaryIO, gateway_fd: int | None) -> _SelectorFeed | _ThreadFeed:
    """Choose how this loop waits, by probing what it has been given.

    Args:
        source: The byte stream to read frames from.
        gateway_fd: What ``GatewayClient.fileno()`` reported.

    Returns:
        A selector over both descriptors where the platform allows it, else
        the narrowest fallback that still serves: a selector over stdin with
        a timer for the gateway, or a reader thread when stdin itself cannot
        be selected on.
    """
    source_fd = _descriptor(source)
    if source_fd is None or not _selectable(source_fd):
        logger.info(
            "MCP adapter: stdin cannot be selected on here, so it is read on "
            "its own thread and notifications go out every %.2fs",
            _POLL_INTERVAL_S,
        )
        return _ThreadFeed(source)
    if gateway_fd is not None and not _selectable(gateway_fd):
        gateway_fd = None
    return _SelectorFeed(source_fd, gateway_fd)


def _descriptor(source: BinaryIO) -> int | None:
    """Return *source*'s descriptor, or ``None`` when it has none.

    Args:
        source: The byte stream to read frames from.

    Returns:
        The descriptor, or ``None`` for a stream that is not backed by one —
        an in-memory buffer in a test, or a stream already closed.
    """
    try:
        return source.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _selectable(fd: int) -> bool:
    """Say whether this platform's ``select`` will wait on *fd*.

    Registering a descriptor proves nothing: ``SelectSelector`` accepts any
    integer and only fails when the wait itself is attempted, which on
    Windows is where a pipe or a console raises. So the descriptor is
    actually waited on, for no time at all.

    Args:
        fd: The descriptor to probe. Nothing is read from it.

    Returns:
        ``True`` when a zero-length wait succeeded.
    """
    selector = selectors.DefaultSelector()
    try:
        selector.register(fd, selectors.EVENT_READ)
        selector.select(0)
    except (OSError, ValueError):
        return False
    finally:
        selector.close()
    return True


def _consume(adapter: McpAdapter, buffer: bytearray, sink: BinaryIO) -> bool:
    """Answer every whole frame in *buffer*.

    Args:
        adapter: The translation.
        buffer: Bytes read but not yet answered; whole frames are removed.
        sink: Where to write the answers.

    Returns:
        ``True`` to keep serving; ``False`` when a frame exceeded the cap
        and the session cannot be trusted to be in step any more.
    """
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            if len(buffer) > MAX_FRAME_BYTES:
                logger.error("MCP adapter: a frame exceeded %d bytes", MAX_FRAME_BYTES)
                return False
            return True
        frame = bytes(buffer[:newline])
        del buffer[: newline + 1]
        if not frame.strip():
            continue
        _answer(adapter, frame, sink)


def _answer(adapter: McpAdapter, frame: bytes, sink: BinaryIO) -> None:
    """Parse one frame, answer it, and write out anything it caused.

    Args:
        adapter: The translation.
        frame: One line of stdin, newline already stripped.
        sink: Where to write.
    """
    try:
        request = json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        _write(sink, _parse_error(f"malformed JSON: {error}"))
        return
    if not isinstance(request, Mapping):
        _write(sink, _parse_error("a JSON-RPC message is an object"))
        return
    response = adapter.handle(request)
    if response is not None:
        _write(sink, response)
    _flush_notifications(adapter, sink)


def _flush_notifications(adapter: McpAdapter, sink: BinaryIO) -> None:
    """Write out every gateway notification waiting, translated.

    Args:
        adapter: The translation.
        sink: Where to write.
    """
    try:
        notifications = adapter.drain_notifications()
    except Exception:  # noqa: BLE001 — a lost event must not end the session
        logger.exception("MCP adapter could not read the gateway's notifications")
        return
    for notification in notifications:
        _write(sink, notification)


def _write(sink: BinaryIO, message: Mapping[str, Any]) -> None:
    """Write one JSON-RPC message and its newline.

    Args:
        sink: Where to write.
        message: The JSON-safe message.
    """
    try:
        line = json.dumps(message, default=str).encode("utf-8") + b"\n"
        sink.write(line)
        sink.flush()
    except (OSError, ValueError):
        logger.exception("MCP adapter could not write a message")


def _parse_error(message: str) -> dict[str, Any]:
    """Build the one error a frame that could not be parsed is answered with.

    Args:
        message: The explanation.

    Returns:
        A JSON-RPC error response with a null id, which is what the
        specification prescribes when the id could not be read.
    """
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": PARSE_ERROR, "message": message},
    }
