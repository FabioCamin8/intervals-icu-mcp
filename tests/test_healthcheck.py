"""Exercise the dependency-free probe over real TCP, including protocol failures."""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from mcp.types import Icon, Tool, ToolAnnotations, ToolExecution
from pydantic import ValidationError

from intervals_icu_mcp import healthcheck
from intervals_icu_mcp.healthcheck import ProbeError, check


@contextmanager
def endpoint(mode="json", tool_updates=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_DELETE(self):
            requests.append(("DELETE", dict(self.headers), None))
            self.send_response(405 if mode == "no-delete" else 204)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", dict(self.headers), body))
            method = body["method"]
            if method == "notifications/initialized":
                self.send_response(500 if mode == "notification-fails" else 202)
                self.end_headers()
                return
            if mode == "target-then-fails" and method == "tools/list" and "params" in body:
                self.send_response(500)
                self.end_headers()
                return
            if mode == "timeout":
                time.sleep(0.3)
            if mode == "redirect":
                self.send_response(307)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
                return
            result: dict[str, Any] = (
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test", "version": "1"},
                }
                if method == "initialize"
                else {
                    "tools": [
                        {"name": "icu_get_athlete_profile", "inputSchema": {"type": "object"}}
                    ]
                }
            )
            if mode == "missing-tool" and method == "tools/list":
                result = {"tools": []}
            if mode in {"paginate", "cursor-loop", "bad-cursor"} and method == "tools/list":
                if "params" not in body or mode != "paginate":
                    result = {"tools": [], "nextCursor": 7 if mode == "bad-cursor" else "next"}
            if mode in {"target-first-page", "target-then-fails"} and method == "tools/list":
                if "params" not in body:
                    result["nextCursor"] = "next"
                else:
                    result = {"tools": []}
            if mode == "bad-init" and method == "initialize":
                result["protocolVersion"] = "unknown"
            if mode == "no-tools-capability" and method == "initialize":
                result["capabilities"] = {}
            if mode == "missing-schema" and method == "tools/list":
                result = {"tools": [{"name": "icu_get_athlete_profile"}]}
            if mode == "bad-tools" and method == "tools/list":
                result = {"tools": [None]}
            if method == "tools/list" and tool_updates is not None:
                result["tools"][0].update(tool_updates)
                if mode == "metadata-other-tool":
                    result["tools"].append(
                        {"name": "icu_get_athlete_profile", "inputSchema": {"type": "object"}}
                    )
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if mode == "wrong-id":
                payload["id"] = 999
            if mode == "rpc-error":
                payload = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32603}}
            content = json.dumps(payload).encode()
            content_type = "application/json"
            if mode in {"sse", "open-sse", "empty-sse", "multi-sse", "trickle", "wrong-event"}:
                content_type = "text/event-stream"
                content = b": keepalive\r\n\r\nevent: message\r\ndata: " + content + b"\r\n\r\n"
                if mode == "wrong-event":
                    content = content.replace(b"event: message", b"event: endpoint")
                if mode == "multi-sse":
                    notification = b'data: {"jsonrpc":"2.0","method":"notifications/test"}\n\n'
                    content = notification + content.replace(b"data: {", b"data: {\ndata: ")
                if mode == "empty-sse":
                    content = b": keepalive\n\n"
            if mode.startswith("newline-"):
                ending = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}[mode.split("-")[1]]
                content_type = "text/event-stream"
                # Two data lines expose accidental extra event boundaries at split CRLF.
                content = ending.join(
                    [
                        b": keepalive",
                        b"",
                        b"event: message",
                        b"data: {",
                        b"data: " + content[1:],
                        b"",
                        b"",
                    ]
                )
            if mode == "mixed-case":
                content_type = "Application/JSON; charset=utf-8"
            if mode == "html":
                content_type, content = "text/html", b"<html>ok</html>"
            if mode == "malformed":
                content = b"not json"
            if mode == "oversized":
                content = b" " * (1024 * 1024 + 1)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if mode != "stateless" and method == "initialize":
                self.send_header("Mcp-Session-Id", "test-session")
            if mode not in {"open-sse", "trickle"} and not mode.endswith("-open"):
                self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                if mode == "trickle":
                    for _ in range(40):
                        self.wfile.write(b":\n")
                        self.wfile.flush()
                        time.sleep(0.01)
                else:
                    self.wfile.write(content)
                    self.wfile.flush()
                    if mode == "open-sse" or mode.endswith("-open"):
                        time.sleep(0.3)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize(
    "mode",
    [
        "json",
        "sse",
        "multi-sse",
        "open-sse",
        "stateless",
        "no-delete",
        "paginate",
        "mixed-case",
        "target-first-page",
    ],
)
def test_ready(mode):
    with endpoint(mode) as (url, requests):
        check(url, 1)
    methods = [body["method"] for verb, _, body in requests if verb == "POST"]
    assert methods == ["initialize", "notifications/initialized", "tools/list"] + (
        ["tools/list"] if mode in {"paginate", "target-first-page"} else []
    )
    assert requests[1][1]["MCP-Protocol-Version"] == "2025-06-18"
    if mode == "stateless":
        assert "Mcp-Session-Id" not in requests[1][1]
        assert all(verb != "DELETE" for verb, _, _ in requests)
    else:
        assert requests[1][1]["Mcp-Session-Id"] == "test-session"
        assert requests[-1][0] == "DELETE"


@pytest.mark.parametrize("ending", ["cr", "lf", "crlf"])
@pytest.mark.parametrize("read_size", [1, 7, 65536])
def test_sse_line_endings_across_reads(ending, read_size, monkeypatch):
    original = http.client.HTTPResponse.read1

    def bounded_read(response, amount=-1):
        return original(response, min(amount, read_size) if amount >= 0 else read_size)

    monkeypatch.setattr(http.client.HTTPResponse, "read1", bounded_read)
    with endpoint(f"newline-{ending}") as (url, requests):
        check(url, 1)
    assert requests[-1][0] == "DELETE"
    assert [body["method"] for verb, _, body in requests if verb == "POST"] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.parametrize("ending", ["cr", "crlf"])
def test_sse_terminal_separator_without_eof(ending):
    with endpoint(f"newline-{ending}-open") as (url, _):
        # Each server response stays open for 0.3 s, longer than the probe deadline.
        check(url, 0.2)


@pytest.mark.parametrize(
    "mode",
    [
        "html",
        "malformed",
        "rpc-error",
        "wrong-id",
        "bad-init",
        "missing-tool",
        "notification-fails",
        "no-tools-capability",
        "bad-tools",
        "missing-schema",
        "wrong-event",
        "target-then-fails",
        "cursor-loop",
        "bad-cursor",
        "empty-sse",
        "redirect",
        "oversized",
    ],
)
def test_protocol_failures(mode):
    with endpoint(mode) as (url, requests):
        with pytest.raises((ProbeError, ValueError)):
            check(url, 1)
    if mode == "redirect":
        assert len(requests) == 1  # Never forwards a session to a redirect target.
    elif mode not in {"html", "redirect"}:
        assert requests[-1][0] == "DELETE"


@pytest.mark.parametrize("mode", ["timeout", "trickle"])
def test_bounded_timeout(mode):
    with endpoint(mode) as (url, _):
        start = time.monotonic()
        with pytest.raises((ProbeError, OSError)):
            check(url, 0.08)
        assert time.monotonic() - start < 0.25


@pytest.mark.parametrize(
    "url,timeout",
    [
        ("ftp://localhost/mcp", 1),
        ("http://user:secret@localhost", 1),
        ("http://localhost/#fragment", 1),
        ("http://localhost", 0),
        ("http://localhost", float("nan")),
        ("http://localhost", float("inf")),
    ],
)
def test_invalid_arguments(url, timeout):
    with pytest.raises(ProbeError):
        check(url, timeout)


def test_refused_and_cli_redaction():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        with pytest.raises(OSError):
            check(url, 1)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "intervals_icu_mcp.healthcheck",
            "--url",
            "http://user:secret@localhost",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stderr == "MCP readiness check failed\n"
    assert "secret" not in result.stdout + result.stderr


@pytest.fixture(scope="module")
def real_server():
    # The actual application, dummy credentials, loopback only. No tool invocation.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("INTERVALS_ICU_")}
    env.update(
        INTERVALS_ICU_API_KEY="test-only",
        INTERVALS_ICU_ATHLETE_ID="i0",
        INTERVALS_ICU_DELETE_MODE="safe",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "intervals_icu_mcp.server",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("isolated server exited before readiness")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("isolated server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_actual_intervals_server(real_server):
    check(real_server, 2)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "intervals_icu_mcp.healthcheck",
            "--url",
            real_server,
            "--timeout",
            "2",
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "MCP ready\n"


INVALID_METADATA = [
    pytest.param({"title": []}, id="title"),
    pytest.param({"description": []}, id="description"),
    pytest.param({"outputSchema": []}, id="output-schema"),
    pytest.param({"_meta": []}, id="meta"),
    pytest.param({"annotations": []}, id="annotations"),
    pytest.param({"annotations": {"title": []}}, id="annotation-title"),
    *[
        pytest.param({"annotations": {hint: []}}, id=hint)
        for hint in ["readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"]
    ],
    pytest.param({"icons": {}}, id="icons"),
    pytest.param({"icons": [None]}, id="icon-object"),
    pytest.param({"icons": [{}]}, id="icon-src-missing"),
    pytest.param({"icons": [{"src": []}]}, id="icon-src"),
    pytest.param(
        {"icons": [{"src": "data:image/png;base64,AA==", "mimeType": []}]}, id="icon-mime"
    ),
    pytest.param(
        {"icons": [{"src": "https://example.com/icon.png", "sizes": {}}]}, id="icon-sizes"
    ),
    pytest.param(
        {"icons": [{"src": "https://example.com/icon.png", "sizes": [None]}]}, id="icon-size-item"
    ),
    pytest.param({"execution": []}, id="execution"),
    pytest.param({"execution": {"taskSupport": "unknown"}}, id="task-support-enum"),
    pytest.param({"execution": {"taskSupport": []}}, id="task-support-type"),
]


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("updates", INVALID_METADATA)
def test_rejects_metadata_also_rejected_by_sdk(updates, mode):
    tool = {"name": "icu_get_athlete_profile", "inputSchema": {"type": "object"}, **updates}
    with pytest.raises(ValidationError):
        Tool.model_validate(tool)
    with endpoint(mode, updates) as (url, requests):
        with pytest.raises(ProbeError, match="invalid MCP tool catalog"):
            check(url, 1)
    assert requests[-1][0] == "DELETE"


VALID_METADATA = [
    {},
    {
        "title": None,
        "description": None,
        "outputSchema": None,
        "icons": None,
        "annotations": None,
        "_meta": None,
        "execution": None,
    },
    {
        "title": "Profile",
        "description": "Read athlete profile",
        "outputSchema": {"type": "object"},
        "_meta": {"vendor": {"arbitrary": [1, False, None]}},
    },
    {
        "annotations": {
            "title": "Read",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    },
    {
        "annotations": {
            "title": None,
            "readOnlyHint": None,
            "destructiveHint": None,
            "idempotentHint": None,
            "openWorldHint": None,
        }
    },
    {
        "icons": [
            {
                "src": "https://example.com/icon.png",
                "mimeType": "image/png",
                "sizes": ["16x16", "any"],
            },
            {"src": "data:image/png;base64,AA==", "mimeType": None, "sizes": None},
        ]
    },
    *[
        {"execution": {"taskSupport": support}}
        for support in [None, "forbidden", "optional", "required"]
    ],
    {"icons": [], "annotations": {}, "execution": {}, "_meta": {}, "outputSchema": {}},
    {
        "vendorExtension": [],
        "annotations": {"vendorExtension": []},
        "icons": [{"src": "https://example.com/icon.png", "vendorExtension": []}],
        "execution": {"vendorExtension": []},
    },
]


@pytest.mark.parametrize("updates", VALID_METADATA)
def test_accepts_valid_metadata_and_extensions(updates):
    tool = {"name": "icu_get_athlete_profile", "inputSchema": {"type": "object"}, **updates}
    Tool.model_validate(tool)
    with endpoint("json", updates) as (url, _):
        check(url, 1)


def test_invalid_metadata_on_another_tool_still_fails():
    with endpoint("metadata-other-tool", {"name": "icu_other", "description": []}) as (url, _):
        with pytest.raises(ProbeError, match="invalid MCP tool catalog"):
            check(url, 1)


def test_known_metadata_fields_stay_in_sync_with_sdk():
    # Dependency upgrades must flag new known fields/enum values for review;
    # unknown vendor extensions remain allowed by the runtime checker.
    assert set(Tool.model_json_schema()["properties"]) == {
        "name",
        "inputSchema",
        *healthcheck._TOOL_OPTIONAL_TYPES,
    }
    assert set(ToolAnnotations.model_json_schema()["properties"]) == set(
        healthcheck._ANNOTATION_TYPES
    )
    assert set(Icon.model_json_schema()["properties"]) == {"src", *healthcheck._ICON_OPTIONAL_TYPES}
    schema = ToolExecution.model_json_schema()
    assert set(schema["properties"]) == {"taskSupport"}
    enum = next(
        branch["enum"]
        for branch in schema["properties"]["taskSupport"]["anyOf"]
        if "enum" in branch
    )
    assert set(enum) == set(healthcheck._TASK_SUPPORT)


@pytest.mark.parametrize(
    "hint", ["readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"]
)
@pytest.mark.parametrize("value", [0, 1, "true", "false"])
def test_requires_wire_boolean_without_sdk_coercion(hint, value):
    # Pydantic accepts/coerces these; the MCP wire format requires actual booleans.
    updates = {"annotations": {hint: value}}
    tool = {"name": "icu_get_athlete_profile", "inputSchema": {}, **updates}
    Tool.model_validate(tool)
    with endpoint("json", updates) as (url, _):
        with pytest.raises(ProbeError, match="invalid MCP tool catalog"):
            check(url, 1)
