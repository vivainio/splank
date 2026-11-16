"""Splunk Observability Cloud (formerly SignalFx) REST API client."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from typing import Iterator

from splank.client import get_ssl_context

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def api_url_for_realm(realm: str) -> str:
    """Derive the API base URL from a realm such as 'eu0' or 'us1'."""
    return f"https://api.{realm.strip()}.observability.splunkcloud.com"


# Observability realm -> MCP gateway region (https://region-<region>.api.scs.splunk.com/...).
MCP_GATEWAY_REGIONS = {
    "eu0": "dub10", "eu1": "fra10", "eu2": "lon10",
    "us0": "iad10", "us1": "pdx10", "us2": "pdx10", "us3": "pdx10",
    "jp0": "tyo10", "au0": "syd10", "sg0": "sin10",
}


def mcp_url_for_realm(realm: str) -> str:
    """Derive the Observability MCP gateway URL from a realm such as 'eu0'."""
    region = MCP_GATEWAY_REGIONS.get(realm.strip().lower())
    if not region:
        raise ValueError(
            f"No known MCP gateway for realm {realm!r}; set o11y_mcp_url on the profile "
            f"(known realms: {', '.join(sorted(MCP_GATEWAY_REGIONS))})"
        )
    return f"https://region-{region}.api.scs.splunk.com/system/mcp-gateway/v1/"


def parse_time_ms(value: str, now: float | None = None) -> int:
    """Parse 'now', a relative time like '-1h', epoch seconds/ms, or an ISO 8601 time to epoch ms."""
    now = time.time() if now is None else now
    value = value.strip()
    if value == "now":
        return int(now * 1000)
    m = re.fullmatch(r"-(\d+)([smhdw])", value)
    if m:
        return int((now - int(m.group(1)) * _UNITS[m.group(2)]) * 1000)
    if re.fullmatch(r"\d+", value):
        n = int(value)
        return n if n > 10**11 else n * 1000  # seconds vs milliseconds
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_duration_ms(value: str) -> int:
    """Parse '30s', '5m', '1h' (or bare milliseconds) to milliseconds."""
    m = re.fullmatch(r"(\d+)([smhdw])", value.strip())
    if m:
        return int(m.group(1)) * _UNITS[m.group(2)] * 1000
    return int(value)


def parse_window(start: str, end: str) -> tuple[int, int]:
    """Parse a before/after window to [start_ms, end_ms). A date-only end includes that whole day."""
    end_ms = parse_time_ms(end)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end.strip()):
        end_ms += 86400 * 1000
    return parse_time_ms(start), end_ms


def compare_windows(
    rows: list[dict],
    group_by: list[str],
    before: tuple[int, int],
    after: tuple[int, int],
    stat: str = "median",
    exclude_weekends: bool = True,
    min_points: int = 5,
    focus: set[str] | None = None,
) -> list[dict]:
    """Compare a statistic of SignalFlow datapoints between two windows, per group.

    ``rows`` are as yielded by ``O11yClient.signalflow``. Groups are keyed by the
    ``group_by`` dimensions; a group needs ``min_points`` datapoints in both windows.
    If ``focus`` is given, each row gets ``role`` = focus/control, where a group is
    "focus" if any of its group-by values (lowercased) is in ``focus``.
    Rows are returned sorted by change_pct (most improved/negative first).
    """
    import statistics

    fn = {"median": statistics.median, "mean": statistics.fmean, "max": max, "min": min}[stat]
    buckets: dict[tuple, dict[str, list[float]]] = {}
    for r in rows:
        ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        if exclude_weekends and ts.weekday() >= 5:
            continue
        ms = int(ts.timestamp() * 1000)
        if before[0] <= ms < before[1]:
            side = "b"
        elif after[0] <= ms < after[1]:
            side = "a"
        else:
            continue
        key = tuple(str(r.get(g, "")) for g in group_by)
        buckets.setdefault(key, {"b": [], "a": []})[side].append(r["value"])

    out = []
    for key, v in buckets.items():
        if len(v["b"]) < min_points or len(v["a"]) < min_points:
            continue
        b, a = fn(v["b"]), fn(v["a"])
        row = dict(zip(group_by, key))
        if focus is not None:
            row["role"] = "focus" if any(k.lower() in focus for k in key) else "control"
        row.update(
            before=round(b, 4),
            after=round(a, 4),
            change_pct=round((a - b) / b * 100, 1) if b else None,
            n_before=len(v["b"]),
            n_after=len(v["a"]),
        )
        out.append(row)
    out.sort(key=lambda r: (r["change_pct"] is None, r["change_pct"] or 0))
    return out


def stream_url_for(api_url: str) -> str:
    """Derive the SignalFlow stream URL from the API URL (api.<realm>... -> stream.<realm>...)."""
    return re.sub(r"^(https?://)api\.", r"\1stream.", api_url)


class O11yClient:
    """Minimal client for the Observability Cloud metadata REST API.

    Authenticates with an org access/user API token sent in ``X-SF-Token``.
    """

    def __init__(self, api_url: str, token: str, verify_ssl: bool = True):
        self.base_url = api_url.rstrip("/")
        self.token = token
        self.ssl_context = get_ssl_context(verify_ssl)

    def _format_http_error(self, e: urllib.error.HTTPError) -> str:
        body = e.read().decode(errors="replace")
        hint = ""
        if e.code == 401:
            hint = "\nCheck o11y_token (an org access token or a user API token) and the realm."
        return f"HTTP {e.code}: {body}{hint}"

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"X-SF-Token": self.token, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(self._format_http_error(e))

    def _get(self, endpoint: str, params: dict | None = None):
        return self._request("GET", endpoint, params)

    def _list(self, endpoint: str, limit: int, **params) -> list[dict]:
        data = self._get(endpoint, {**params, "limit": limit})
        # Most endpoints wrap results in {"count", "results"}; incidents returns a bare list.
        return data if isinstance(data, list) else data.get("results", [])

    def metrics(self, query: str | None, limit: int = 100) -> list[dict]:
        """Search metric names (e.g. query='sf_metric:cpu*')."""
        return self._list("/v2/metric", limit, query=query)

    def metric_time_series(self, query: str | None, limit: int = 100) -> list[dict]:
        """Search metric time series metadata (e.g. query='sf_metric:cpu.utilization')."""
        return self._list("/v2/metrictimeseries", limit, query=query)

    def detectors(self, name: str | None, limit: int = 100) -> list[dict]:
        """Search detectors by name."""
        return self._list("/v2/detector", limit, name=name)

    # The incident API rejects offset+limit beyond this (HTTP 500).
    INCIDENT_WINDOW = 10000

    def incidents(
        self,
        include_resolved: bool = False,
        limit: int = 100,
        detector: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[dict]:
        """List incidents, newest first (active only unless include_resolved).

        ``detector`` is a case-insensitive substring of the detector name;
        ``since_ms``/``until_ms`` bound ``anomalyStateUpdateTimestamp`` (the time of the
        incident's last state change, i.e. when it resolved if it has). Only the newest
        ~10000 incidents are reachable through the API.
        """
        page = min(max(limit, 1), 1000)
        needle = detector.lower() if detector else None
        out: list[dict] = []
        offset = 0
        while len(out) < limit and offset < self.INCIDENT_WINDOW:
            batch = self._get(
                "/v2/incident",
                {
                    "limit": min(page, self.INCIDENT_WINDOW - offset),
                    "offset": offset or None,
                    "includeResolved": "true" if include_resolved else None,
                },
            )
            batch = batch if isinstance(batch, list) else batch.get("results", [])
            if not batch:
                break
            for inc in batch:
                ts = inc.get("anomalyStateUpdateTimestamp", 0)
                if (until_ms and ts > until_ms) or (since_ms and ts < since_ms):
                    continue
                if needle and needle not in inc.get("detectorName", "").lower():
                    continue
                out.append(inc)
                if len(out) >= limit:
                    break
            oldest = min(i.get("anomalyStateUpdateTimestamp", 0) for i in batch)
            if len(batch) < page or (since_ms and oldest < since_ms):
                break  # last page, or everything further is older than --since
            offset += page
        return out

    def services(self, start_ms: int, stop_ms: int) -> dict:
        """APM service topology (nodes and edges) for a time window."""
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = json.dumps({"timeRange": f"{iso(start_ms)}/{iso(stop_ms)}"}).encode()
        data = self._request(
            "POST", "/v2/apm/topology", body=body, content_type="application/json"
        )
        return data.get("data", data)

    def trace(self, trace_id: str) -> list[dict]:
        """All spans of the latest segment of an APM trace."""
        return self._request("GET", f"/v2/apm/trace/{urllib.parse.quote(trace_id)}/latest")

    def signalflow(
        self,
        program: str,
        start_ms: int,
        stop_ms: int,
        resolution_ms: int | None = None,
    ) -> Iterator[dict]:
        """Run a SignalFlow program over a window; yield one row per datapoint.

        Each row has ``timestamp`` (ISO 8601 UTC), ``value``, the stream ``label``,
        the originating ``metric`` and the time series' dimensions.
        """
        params = {"start": start_ms, "stop": stop_ms, "immediate": "true"}
        if resolution_ms:
            params["resolution"] = resolution_ms
        url = f"{stream_url_for(self.base_url)}/v2/signalflow/execute?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            data=program.encode(),
            headers={"X-SF-Token": self.token, "Content-Type": "text/plain", "Accept": "text/event-stream"},
            method="POST",
        )
        series: dict[str, dict] = {}
        try:
            resp = urllib.request.urlopen(req, context=self.ssl_context)
        except urllib.error.HTTPError as e:
            raise RuntimeError(self._format_http_error(e))

        with resp:
            event = None
            for raw in resp:
                line = raw.decode().rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:])
                if event == "metadata":
                    series[payload["tsId"]] = _series_labels(payload.get("properties", {}))
                elif event == "data":
                    ts = datetime.fromtimestamp(payload["logicalTimestampMs"] / 1000, timezone.utc)
                    for point in payload.get("data", []):
                        yield {
                            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            **series.get(point["tsId"], {"tsId": point["tsId"]}),
                            "value": point["value"],
                        }
                elif event == "error":
                    raise RuntimeError(f"SignalFlow error: {payload.get('message', payload)}")
                elif event == "control-message" and payload.get("event") in ("END_OF_CHANNEL", "CHANNEL_ABORT"):
                    return


def _series_labels(props: dict) -> dict:
    """Pick the human-relevant labels from a SignalFlow time series' metadata."""
    labels = {"label": props.get("sf_streamLabel"), "metric": props.get("sf_originatingMetric")}
    dims = list(props.get("sf_key", [])) + list(props.get("sf_singletonFixedDimensions", []))
    for key in dims:
        # Skip SignalFlow's own bookkeeping, but keep real dimensions that happen to start with sf_ (sf_service, ...).
        if key in ("sf_metric", "sf_originatingMetric", "computationId") or key not in props:
            continue
        labels[key] = props[key]
    return {k: v for k, v in labels.items() if v is not None}


# ---------------------------------------------------------------------------
# Observability MCP gateway (APM trace discovery)
# ---------------------------------------------------------------------------

MCP_TOOL_PREFIX = "o11y_"


def call_mcp_tool(mcp, name: str, params: dict | None = None):
    """Call an Observability MCP tool (arguments go inside a `params` object) and decode its JSON text."""
    result = mcp.call_tool(MCP_TOOL_PREFIX + name, {"params": params or {}})
    texts = [b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text"]
    text = "\n".join(texts)
    if result.get("isError") or text.startswith("Invalid arguments"):
        raise RuntimeError(text or f"MCP tool {name} failed")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected response from MCP tool {name}: {text[:300]}")


def apm_environments(mcp) -> list[str]:
    """Names of the APM environments."""
    data = call_mcp_tool(mcp, "get_apm_environments")
    return [e["name"] for e in data.get("environments", [])]


def exemplar_traces(
    mcp,
    service: str,
    environment: str,
    start: str = "-1h",
    stop: str = "now",
    kind: str = "req",
    min_ms: float | None = None,
    max_ms: float | None = None,
    tags: dict[str, list[str]] | None = None,
) -> tuple[list[dict], dict]:
    """Sample traces for a service (kind: req = any, err = has errors, rc_err = root-cause errors).

    Returns (rows, status). The server returns only a handful of samples and may time out
    on busy services (status.is_partial); narrow the time range or add filters then.
    """
    params: dict = {
        "environment_name": environment,
        "service_name": service,
        "exemplar_type": kind,
        "time_range": {"start": start, "stop": stop},
    }
    if min_ms is not None:
        params["min_latency_micros"] = int(min_ms * 1000)
    if max_ms is not None:
        params["max_latency_micros"] = int(max_ms * 1000)
    if tags:
        params["tags_or_dimensions"] = tags
    data = call_mcp_tool(mcp, "get_apm_exemplar_traces", params)

    rows = []
    for entry in data.get("items", []):
        it = entry.get("item", entry)
        errors = [
            c["service"] + (" (root cause)" if any(e.get("isRootCause") for e in c.get("errors", [])) else "")
            for c in it.get("serviceSpanCounts", [])
            if c.get("errors")
        ]
        rows.append({
            "traceId": it.get("traceId"),
            "start": datetime.fromtimestamp(it["startTimeMicros"] / 1e6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if it.get("startTimeMicros") else None,
            "service": it.get("initiatingService"),
            "operation": it.get("initiatingOperation"),
            "durationMs": round(it.get("durationMicros", 0) / 1000, 3),
            "errorServices": errors,
            "spansByService": {c["service"]: c["spanCount"] for c in it.get("serviceSpanCounts", [])},
            "link": (it.get("link") or {}).get("url"),
        })
    status = data.get("status") or {}
    if data.get("warning"):
        status = {**status, "warning": data["warning"]}
    return rows, status


# ---------------------------------------------------------------------------
# Generic access to every Observability MCP tool
# ---------------------------------------------------------------------------


def resolve_tool(tools: list[dict], name: str) -> dict:
    """Find a tool by exact name, with or without the gateway's ``o11y_`` prefix."""
    by_name = {t["name"]: t for t in tools}
    for candidate in (name, MCP_TOOL_PREFIX + name):
        if candidate in by_name:
            return by_name[candidate]
    close = sorted(n for n in by_name if name.lower() in n.lower())[:8]
    hint = f" Did you mean: {', '.join(close)}?" if close else " Run 'splank oy tools' to list them."
    raise RuntimeError(f"Unknown tool {name!r}.{hint}")


def tool_inputs(tool: dict) -> tuple[bool, dict, list[str]]:
    """Return (wrapped, properties, required) for a tool's inputs.

    Gateway tools usually take a single ``params`` object; ``wrapped`` is True then and
    ``properties``/``required`` describe the fields inside it, so callers can use them directly.
    """
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    inner = props.get("params")
    if set(props) == {"params"} and isinstance(inner, dict) and inner.get("properties") is not None:
        return True, inner.get("properties") or {}, inner.get("required") or []
    return False, props, schema.get("required") or []


def describe_fields(props: dict, required: list[str] | None = None, prefix: str = "") -> list[dict]:
    """Flatten a JSON-schema properties dict into rows (nested objects become dotted names)."""
    rows = []
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        name = f"{prefix}{key}"
        kind = spec.get("type")
        if kind is None and spec.get("anyOf"):
            kind = "|".join(str(o.get("type")) for o in spec["anyOf"] if isinstance(o, dict))
        desc = " ".join((spec.get("description") or "").split())
        rows.append({
            "field": name,
            "type": kind,
            "required": key in (required or []) if not prefix else None,
            "default": spec.get("default"),
            "description": desc[:160] + ("..." if len(desc) > 160 else ""),
        })
        if isinstance(spec.get("properties"), dict):
            rows.extend(describe_fields(spec["properties"], spec.get("required"), prefix=name + "."))
    return rows


def _coerce(value: str):
    """Parse a command-line value as JSON when possible, else keep the string."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def build_tool_arguments(tool: dict, pairs: list[str], json_args: str | None = None) -> dict:
    """Build the ``arguments`` for tools/call from key=value pairs (dotted keys nest).

    ``time_range.start=-1h`` becomes ``{"time_range": {"start": "-1h"}}``; values are JSON-decoded
    when they parse (numbers, true/false, lists, objects). For gateway tools that wrap their inputs in
    ``params`` the wrapper is added automatically. ``json_args`` supplies a base object that pairs override.
    """
    wrapped, _, _ = tool_inputs(tool)
    args: dict = json.loads(json_args) if json_args else {}
    if wrapped and set(args) == {"params"}:
        args = args["params"]  # user already supplied the wrapper
    if not isinstance(args, dict):
        raise ValueError("--json must be a JSON object")
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"argument {pair!r} is not in key=value form")
        node = args
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"cannot set {key!r}: {part!r} is not an object")
        node[leaf] = _coerce(value)
    return {"params": args} if wrapped else args
