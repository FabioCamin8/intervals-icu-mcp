"""Dependency-free readiness probe for the Streamable HTTP transport.

Only initializes MCP and lists tools; never invokes an Intervals.icu tool.
"""

import argparse
import http.client
import json
import math
import sys
import time
from typing import Any, cast
from urllib.parse import urlsplit

_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
_MAX_BODY = 1024 * 1024


class ProbeError(Exception):
    """The endpoint did not demonstrate MCP readiness."""


_TOOL_OPTIONAL_TYPES: dict[str, type[Any]] = {
    "title": str,
    "description": str,
    "outputSchema": dict,
    "icons": list,
    "annotations": dict,
    "_meta": dict,
    "execution": dict,
}
_ANNOTATION_TYPES: dict[str, type[Any]] = {
    "title": str,
    "readOnlyHint": bool,
    "destructiveHint": bool,
    "idempotentHint": bool,
    "openWorldHint": bool,
}
_ICON_OPTIONAL_TYPES: dict[str, type[Any]] = {"mimeType": str, "sizes": list}
_TASK_SUPPORT = ("forbidden", "optional", "required")


def _optional_types(value: dict[str, Any], fields: dict[str, type[Any]]) -> bool:
    return all(
        value.get(name) is None or isinstance(value[name], expected)
        for name, expected in fields.items()
    )


def _valid_tool(value: Any) -> bool:
    """Validate known MCP Tool wire fields without SDK imports or coercion.

    Unknown extension fields are allowed, as in the MCP SDK. Input/output
    schemas and _meta are arbitrary objects, not schemas to evaluate here.
    """
    if not isinstance(value, dict):
        return False
    tool = cast(dict[str, Any], value)
    if (
        not isinstance(tool.get("name"), str)
        or not isinstance(tool.get("inputSchema"), dict)
        or not _optional_types(tool, _TOOL_OPTIONAL_TYPES)
    ):
        return False
    annotations = tool.get("annotations")
    if annotations is not None and not _optional_types(annotations, _ANNOTATION_TYPES):
        return False
    icons = tool.get("icons")
    if icons is not None:
        for value in icons:
            if not isinstance(value, dict):
                return False
            icon = cast(dict[str, Any], value)
            if not isinstance(icon.get("src"), str) or not _optional_types(
                icon, _ICON_OPTIONAL_TYPES
            ):
                return False
            sizes = icon.get("sizes")
            if sizes is not None and not all(isinstance(size, str) for size in sizes):
                return False
    execution = tool.get("execution")
    return execution is None or execution.get("taskSupport") in (None, *_TASK_SUPPORT)


class _Probe:
    def __init__(self, url: str, timeout: float):
        target = urlsplit(url)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.fragment
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ProbeError("invalid probe URL or timeout")
        self.target = target
        self.path = target.path or "/"
        if target.query:
            self.path += "?" + target.query
        self.deadline = time.monotonic() + timeout
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("probe deadline exceeded")
        return remaining

    @staticmethod
    def result(payload: Any, request_id: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProbeError("invalid MCP response")
        payload = cast(dict[str, Any], payload)
        if (
            payload.get("jsonrpc") != "2.0"
            or type(payload.get("id")) is not int
            or payload["id"] != request_id
            or "error" in payload
            or not isinstance(payload.get("result"), dict)
        ):
            raise ProbeError("invalid MCP response")
        return payload["result"]

    def request(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        connection_type = (
            http.client.HTTPSConnection
            if self.target.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            self.target.hostname or "", self.target.port, timeout=self.remaining()
        )
        try:
            connection.connect()
            sock = connection.sock
            assert sock is not None
            sock.settimeout(self.remaining())
            data = json.dumps(body).encode() if body is not None else None
            connection.request(method, self.path, data, self.headers)
            sock.settimeout(self.remaining())
            response = connection.getresponse()
            with response:
                if method == "DELETE":
                    # Session termination is optional in Streamable HTTP.
                    if response.status not in {200, 204, 405}:
                        raise ProbeError("MCP session cleanup failed")
                    return {}
                if body is None:
                    raise ProbeError("missing MCP request")
                request_id = body.get("id")
                if request_id is None:
                    if response.status != 202:
                        raise ProbeError("MCP initialization notification rejected")
                    return {}
                if response.status != 200:
                    raise ProbeError("MCP request failed")
                session = response.getheader("Mcp-Session-Id")
                if body.get("method") == "initialize" and session:
                    self.headers["Mcp-Session-Id"] = session
                content_type = (
                    response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                )
                if content_type not in {"application/json", "text/event-stream"}:
                    raise ProbeError("unsupported MCP response content type")
                buffer = b""
                size = 0
                event_data: list[bytes] = []
                event_type = b"message"
                skip_lf = False
                while True:
                    self.remaining()
                    if response.isclosed():
                        chunk = b""
                    else:
                        sock.settimeout(self.remaining())
                        chunk = response.read1(65536)
                    size += len(chunk)
                    if size > _MAX_BODY:
                        raise ProbeError("MCP response too large")
                    eof = not chunk
                    if content_type == "text/event-stream":
                        # CR ends a line immediately; swallow a following LF even
                        # when CRLF is split across reads. Count raw bytes above.
                        if skip_lf:
                            chunk = chunk.removeprefix(b"\n")
                        skip_lf = chunk.endswith(b"\r")
                        chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    buffer += chunk
                    if content_type == "application/json":
                        if eof:
                            return self.result(json.loads(buffer), request_id)
                    else:
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if line.startswith(b"event:"):
                                event_type = line[6:].removeprefix(b" ")
                            elif line.startswith(b"data:"):
                                value = line[5:]
                                event_data.append(value[1:] if value.startswith(b" ") else value)
                            elif not line:
                                if event_data and event_type == b"message":
                                    payload = json.loads(b"\n".join(event_data))
                                    if isinstance(payload, dict) and "id" in payload:
                                        return self.result(payload, request_id)
                                event_data = []
                                event_type = b"message"
                        if eof:
                            raise ProbeError("MCP stream ended without a response")
        finally:
            connection.close()


def check(url: str = "http://127.0.0.1:8000/mcp", timeout: float = 10.0) -> None:
    """Require a valid MCP handshake and the athlete-profile tool in the catalog."""
    probe = _Probe(url, timeout)
    try:
        initialized = probe.request(
            "POST",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "intervals-icu-healthcheck", "version": "1"},
                },
            },
        )
        capabilities: Any = initialized.get("capabilities")
        server: Any = initialized.get("serverInfo")
        if (
            not isinstance(initialized.get("protocolVersion"), str)
            or initialized["protocolVersion"] not in _PROTOCOLS
            or not isinstance(capabilities, dict)
            or not isinstance(cast(dict[str, Any], capabilities).get("tools"), dict)
            or not isinstance(server, dict)
            or not isinstance(cast(dict[str, Any], server).get("name"), str)
            or not isinstance(cast(dict[str, Any], server).get("version"), str)
        ):
            raise ProbeError("invalid MCP initialization result")
        probe.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        probe.request("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        cursor: str | None = None
        seen: set[str] = set()
        request_id = 2
        target_seen = False
        while True:
            request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
            if cursor is not None:
                request["params"] = {"cursor": cursor}
            result = probe.request("POST", request)
            tools = result.get("tools")
            if not isinstance(tools, list) or any(not _valid_tool(tool) for tool in tools):
                raise ProbeError("invalid MCP tool catalog")
            target_seen |= any(tool["name"] == "icu_get_athlete_profile" for tool in tools)
            next_cursor: Any = result.get("nextCursor")
            if next_cursor is None:
                if target_seen:
                    return
                raise ProbeError("expected MCP tool missing")
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise ProbeError("invalid MCP pagination cursor")
            cursor = next_cursor
            seen.add(cursor)
            request_id += 1
    finally:
        if "Mcp-Session-Id" in probe.headers:
            try:
                probe.request("DELETE")
            except (ProbeError, OSError, ValueError, http.client.HTTPException):
                # Cleanup cannot hide a failed probe or override readiness.
                pass


def main() -> int:
    """Exit zero on readiness, one on transport/protocol failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--timeout", type=float, default=10.0, help="overall deadline in seconds")
    args = parser.parse_args()
    try:
        check(args.url, args.timeout)
    except (ProbeError, OSError, ValueError, http.client.HTTPException):
        # Do not print response bodies, URLs, session identifiers or credentials.
        print("MCP readiness check failed", file=sys.stderr)
        return 1
    print("MCP ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
