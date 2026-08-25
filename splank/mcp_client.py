"""Minimal MCP (Model Context Protocol) client over the Streamable HTTP transport.

Splunk exposes an MCP server at a URL like
``https://<host>/en-US/splunkd/__raw/services/mcp``, separate from the
regular management-port REST API used by :mod:`splank.client`. It typically
requires its own bearer token (the token's audience must match the MCP
service - a plain Splunk API token is usually rejected).
"""

import json
import ssl
import urllib.error
import urllib.request

from splank import __version__

PROTOCOL_VERSION = "2025-06-18"


def get_ssl_context(verify: bool = True) -> ssl.SSLContext:
    """Create SSL context."""
    if verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class MCPClient:
    """JSON-RPC client for an MCP server's Streamable HTTP transport."""

    def __init__(self, url: str, token: str | None = None, verify_ssl: bool = True):
        self.url = url
        self.token = token
        self.ssl_context = get_ssl_context(verify_ssl)
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False

    def _request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _post(self, payload: dict) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=self.ssl_context) as resp:
                session_id = resp.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")
        return self._parse_body(raw)

    @staticmethod
    def _parse_body(raw: str) -> dict | None:
        raw = raw.strip()
        if not raw:
            return None
        if raw.startswith(("event:", "data:")) or "\ndata:" in raw:
            data_lines = [
                line[len("data:"):].strip()
                for line in raw.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                return None
            raw = data_lines[-1]
        return json.loads(raw)

    def _call(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and return its "result" (raises on "error")."""
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id(),
                "method": method,
                "params": params or {},
            }
        )
        if not response:
            raise RuntimeError(f"No response from MCP server for {method!r}")
        if "error" in response:
            err = response["error"]
            raise RuntimeError(f"{err.get('message', 'MCP error')} (code {err.get('code')})")
        return response.get("result", {})

    def initialize(self) -> dict:
        """Perform the MCP initialize handshake (idempotent)."""
        if self._initialized:
            return {}
        result = self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "splank", "version": __version__},
            },
        )
        # Fire-and-forget notification; no "id", no response expected.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return result

    def list_tools(self) -> list[dict]:
        """Return the list of tools the MCP server exposes."""
        self.initialize()
        return self._call("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool by name and return its result."""
        self.initialize()
        return self._call("tools/call", {"name": name, "arguments": arguments or {}})
