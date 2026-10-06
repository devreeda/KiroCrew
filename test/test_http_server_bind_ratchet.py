"""Ratchet: no new bare ``HTTPServer((host, port), ...)`` construction.

``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` between
``bind()`` and ``listen()``. On a macOS runner that system-resolver lookup can
stall for longer than a parent's start deadline while the socket is bound but
refuses every connect, and the macOS shard goes red with
``runtime stayed {'state': 'starting'}``. ``plumb_cpu.py`` and the real-spawn
stand-in avoid it by overriding ``server_bind``.

testing-conventions.md ("``http.server`` looks the host up between ``bind()``
and ``listen()``") names the fix: subclass and call
``socketserver.TCPServer.server_bind`` so no name is resolved. The count is
exact, so a removed site records the new number in the same commit; a new
site either subclasses or records the number with its reason in the PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("tree_scan_http_server_bind_ratchet")

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED = (_ROOT / "src" / "kiro_crew", _ROOT / "test")

# A direct construction of the stdlib class, the shape whose server_bind resolves
# the host. A subclass (`class S(ThreadingHTTPServer)`) is not a construction.
_BARE = re.compile(r"\b(?:Threading)?HTTPServer\(\s*\(")

_BASELINE_SITES = 22


def _sites() -> list[str]:
    found: list[str] = []
    for base in _SCANNED:
        for path in sorted(base.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if _BARE.search(line):
                    found.append(f"{path.relative_to(_ROOT)}:{number}: {line.strip()}")
    return found


def test_the_needle_matches_the_shipped_shape() -> None:
    assert _BARE.search('ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()')
    assert _BARE.search('srv = http.server.HTTPServer(("127.0.0.1", 0), _H)')
    assert not _BARE.search("class LoopbackServer(ThreadingHTTPServer):")
    assert not _BARE.search('LoopbackServer(("127.0.0.1", args.port), AttestedHandler)')


def test_bare_http_server_constructions_match_the_recorded_count() -> None:
    sites = _sites()
    assert len(sites) == _BASELINE_SITES, (
        f"{len(sites)} bare HTTPServer((host, port), ...) construction(s) "
        f"(recorded: {_BASELINE_SITES}). Its server_bind resolves the host with "
        "socket.getfqdn, which can stall on macOS while the socket refuses connects. "
        "Subclass and call socketserver.TCPServer.server_bind (see plumb_cpu.py), or, "
        "if you REMOVED a site, set _BASELINE_SITES to the new number.\n" + "\n".join(sites)
    )
