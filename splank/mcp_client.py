"""Minimal MCP (Model Context Protocol) client over the Streamable HTTP transport.

Splunk exposes an MCP server at a URL like
``https://<host>/en-US/splunkd/__raw/services/mcp``, separate from the
regular management-port REST API used by :mod:`splank.client`. It typically
requires its own bearer token (the token's audience must match the MCP
service - a plain Splunk API token is usually rejected).
"""

import json
import ssl
import sys
import urllib.error
import urllib.request
from typing import Iterator

from splank import __version__
from splank.client import build_spl

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

    def __init__(
        self,
        url: str,
        token: str | None = None,
        verify_ssl: bool = True,
        headers: dict[str, str] | None = None,
        timeout: float = 120,
    ):
        self.url = url
        self.token = token
        self.extra_headers = dict(headers or {})
        self.timeout = timeout
        self.ssl_context = get_ssl_context(verify_ssl)
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False
        self._tool_schemas: dict[str, dict] | None = None

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
        headers.update(self.extra_headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=self.timeout) as resp:
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

    # ------------------------------------------------------------------
    # Search (a SplunkClient.search-compatible interface over MCP)
    # ------------------------------------------------------------------

    SEARCH_TOOL = "splunk_run_query"

    def _tool_properties(self, name: str) -> dict:
        """Return the `inputSchema.properties` of a tool (empty if unknown)."""
        if self._tool_schemas is None:
            self._tool_schemas = {t["name"]: t for t in self.list_tools()}
        tool = self._tool_schemas.get(name)
        if tool is None:
            raise RuntimeError(f"MCP server does not expose a {name!r} tool")
        return (tool.get("inputSchema") or {}).get("properties") or {}

    @staticmethod
    def _rows_from_payload(payload) -> list[dict] | None:
        """Pull a list of row dicts out of a decoded JSON payload, if it has one."""
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("results", "rows", "events"):
                if isinstance(payload.get(key), list):
                    return [r for r in payload[key] if isinstance(r, dict)]
        return None

    @classmethod
    def parse_search_result(cls, result: dict) -> list[dict]:
        """Extract result rows from a tools/call result for a search tool."""
        texts = [
            b.get("text", "")
            for b in result.get("content") or []
            if b.get("type") == "text"
        ]
        if result.get("isError"):
            raise RuntimeError("\n".join(texts) or "MCP search tool reported an error")

        rows = cls._rows_from_payload(result.get("structuredContent"))
        if rows is not None:
            return rows

        for text in texts:
            try:
                rows = cls._rows_from_payload(json.loads(text))
            except json.JSONDecodeError:
                continue
            if rows is not None:
                return rows

        snippet = (texts[0] if texts else json.dumps(result))[:300]
        raise RuntimeError(f"Could not parse rows from MCP search result: {snippet}")

    def search(
        self,
        query: str,
        earliest: str = "-24h",
        latest: str = "now",
        max_results: int = 100,
        stream: bool = False,  # accepted for SplunkClient compatibility; MCP can't stream
    ) -> Iterator[dict]:
        """Run a search through the MCP server's query tool and yield result rows."""
        spl = build_spl(query, max_results)
        props = self._tool_properties(self.SEARCH_TOOL)

        arguments: dict = {"query": spl}
        if "row_limit" in props:
            arguments["row_limit"] = max_results

        # Pass the time range as tool arguments if the tool has them; otherwise
        # fall back to inlining it into the SPL (only possible for `search ...`).
        earliest_key = next((k for k in props if k.lower().startswith("earliest")), None)
        latest_key = next((k for k in props if k.lower().startswith("latest")), None)
        if earliest_key:
            arguments[earliest_key] = earliest
        if latest_key:
            arguments[latest_key] = latest
        if not (earliest_key or latest_key):
            if spl.lstrip().startswith("search "):
                spl = spl.replace(
                    "search ", f"search earliest={earliest} latest={latest} ", 1
                )
                arguments["query"] = spl
            else:
                print(
                    f"Warning: {self.SEARCH_TOOL} has no time-range arguments and the query "
                    "starts with '|'; --earliest/--latest are ignored (put them in the SPL).",
                    file=sys.stderr,
                )

        result = self.call_tool(self.SEARCH_TOOL, arguments)
        yield from self.parse_search_result(result)
