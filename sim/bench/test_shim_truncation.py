#!/usr/bin/env python3
"""A truncated model stream must still LOOK truncated after the shim relays it.

THE HAZARD. Google serves the turn stream as HTTP/1.1 with
`Transfer-Encoding: chunked`. Chunked framing is self-terminating: the body
ends with a zero-length chunk, so a stream that dies early is DETECTABLE, and
httpx raises `RemoteProtocolError("peer closed connection without sending
complete message body")`. The brain then fails the turn and retries -- loudly,
correctly.

If a relay re-frames that response as close-delimited (HTTP/1.0, no
Content-Length, no Transfer-Encoding), "the connection closed" IS the
end-of-message signal. A truncated body becomes byte-identical to a complete
one. No exception reaches the brain; `_sse_chunks` yields the parts that did
arrive; `absorb()` commits them to history as if the model had finished.

Concretely: the model plans `navigate(counter)` then `pick_any_object(mug)`
then stops. The stream dies after the second event. The robot drives to the
counter, never picks up the mug, and its own conversation history says that was
the whole answer. Nothing is logged. The final chunk also carries
`usageMetadata`, so the turn is never metered and the benchmark's cost figure
under-reports it.

This drives the REAL handler against a fake upstream that dies mid-body, and
asserts the client can still tell. Runs offline; costs nothing.

  usage: test_shim_truncation.py     (exit 0 = all pass)
"""

from __future__ import annotations

import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini_shim  # noqa: E402

FAILURES: list[str] = []
EVENTS = [b'data: {"seg": 0}\n\n', b'data: {"seg": 1}\n\n']


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok   ' if ok else 'FAIL '} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


class DyingUpstream(threading.Thread):
    """Serves chunked SSE and hangs up mid-body, without the final 0 chunk.

    Raw sockets rather than http.server: the whole point is emitting framing
    that a well-behaved server never would."""

    daemon = True

    def __init__(self) -> None:
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n"
                         b"Content-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")
            for event in EVENTS:
                conn.sendall(f"{len(event):X}\r\n".encode() + event + b"\r\n")
            # and then it dies: no terminating "0\r\n\r\n"
        except OSError:
            pass
        finally:
            conn.close()


def raw_response(host: str, port: int, path: str) -> tuple[bytes, bytes]:
    """(header block, body bytes) straight off the socket, no client library.

    Asserting on the FRAMING rather than on some client's reaction is the whole
    point. `urllib` happily tolerates a chunked body that never terminates --
    it swallows the truncation the same way the shim did -- so using it as the
    control proves nothing. httpx/h11, which is what the brain actually uses,
    is strict. Rather than depend on which client is stricter, check the
    invariant directly: does the relayed response carry self-terminating
    framing, and is the terminator present only when the upstream really
    finished?
    """
    sock = socket.create_connection((host, port), timeout=20)
    sock.sendall(f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
                 f"Content-Length: 2\r\nContent-Type: application/json\r\n\r\n{{}}".encode())
    buffer = b""
    while True:
        try:
            piece = sock.recv(65536)
        except OSError:
            break
        if not piece:
            break
        buffer += piece
    sock.close()
    head, _, body = buffer.partition(b"\r\n\r\n")
    return head, body


def main() -> int:
    print("shim truncation:")
    upstream = DyingUpstream()
    upstream.start()
    gemini_shim.UPSTREAM = f"http://127.0.0.1:{upstream.port}"
    gemini_shim.Handler.api_key = "test-key-not-real"

    server = ThreadingHTTPServer(("127.0.0.1", 0), gemini_shim.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    head, body = raw_response("127.0.0.1", server.server_port,
                              "/v1beta/models/m:streamGenerateContent?alt=sse")
    lowered = head.lower()

    check("both SSE events were relayed", body.count(b"data: ") == len(EVENTS),
          f"{body.count(b'data: ')} of {len(EVENTS)}")

    # Self-terminating framing is the whole defence. Close-delimited (HTTP/1.0,
    # no length, no transfer-encoding) makes truncation indistinguishable from
    # completion, because the close IS the terminator.
    self_terminating = b"transfer-encoding: chunked" in lowered or b"content-length:" in lowered
    check("the relayed response uses self-terminating framing",
          self_terminating,
          head.decode(errors="replace").splitlines()[0] if head else "no head")

    # Upstream died without its final chunk, so ours must be missing too --
    # that absence is exactly what makes a strict client raise.
    check("a truncated upstream yields no terminating chunk",
          self_terminating and not body.rstrip().endswith(b"0"),
          "body ends: " + repr(body[-24:]))

    # The mid-stream error path must not append a second status line into a
    # body whose headers were already flushed. `_sse_chunks` drops any line
    # not starting with "data: ", so injected junk vanishes without a trace --
    # which is what turns a relay failure into a silent one.
    check("no HTTP status line is injected into the relayed body",
          b"HTTP/1." not in body,
          repr(body[-120:]))

    server.shutdown()
    print(f"\n{'FAILED' if FAILURES else 'all pass'}"
          f" ({len(FAILURES)} failure{'s' if len(FAILURES) != 1 else ''})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
