# Splank

CLI tool for querying Splunk logs.

## Install

```bash
uv tool install splank
```

## Setup

```bash
splank init
```

This creates `~/.config/splank/credentials.toml` with your Splunk credentials.

To add a single profile without opening an editor, pass a name (and optionally a host) instead:

```bash
splank init qa mystack   # bare stack names expand to <name>.splunkcloud.com
splank init qa splunk-qa.example.com
```

This runs a short wizard (token only — it prompts just for the token), tests the connection before saving, and appends the profile to your existing credentials file (creating it if needed). It offers to overwrite if the name already exists, and to set the new profile as `default_profile`.

### Configuration

The credentials file supports multiple profiles. Each profile authenticates either with a Splunk auth `token` (HTTP `Authorization: Bearer …`) or with `username` + `password` — set one or the other:

```toml
default_profile = "prod"

[profiles.prod]
host = "splunk.example.com"
port = 8089
token = "your-token-here"
verify_ssl = true

[profiles.qa]
host = "splunk-qa.example.com"
port = 8089
username = "admin"
password = "changeme"
verify_ssl = true
```

## Usage

```bash
# Search (uses default profile)
splank search 'index=main Level=ERROR' -m 10

# Search using specific profile
splank -p qa search 'index=main Level=ERROR'

# Paste a Splunk web UI URL directly — query and time range are extracted
splank search 'https://splunk.example.com/en-US/app/search/search?q=search%20index%3Dmain&earliest=-30d%40d&latest=now'

# Discover indexes
splank discover 'web*'

# Discover with field info
splank discover 'app-*' --fields -o DISCOVERY.md

# Manage jobs
splank jobs
splank clear
```

## Commands

- `init` - Create credentials file, or `init <name> [host]` to add one profile via a short token-only wizard
- `search` - Execute SPL query
- `discover` - Discover available indexes
- `jobs` - List search jobs
- `clear` - Clear my search jobs
- `mcp` - Interact with a Splunk MCP server (see below)
- `oy` - Query Splunk Observability Cloud / SignalFx: metrics, SignalFlow, detectors, incidents, APM services and traces (full command list below)
- `ask` - Ask a free-form question; generates SPL via MCP/SAIA and runs it (see below)

## Observability Cloud (SignalFx)

Splunk Observability Cloud (formerly SignalFx) is Splunk's metrics, traces and alerting platform. It is a
separate product from the Splunk search you use with `search`: it has its own API and its own token,
separate from the Splunk `token` and `mcp_token`. All of it lives under one command, `splank oy`.

Each profile can carry `o11y_realm` and `o11y_token`; a profile may hold only these (no Splunk `host`).
Find the realm in your Observability URL (`https://app.<realm>.observability.splunkcloud.com`) and create an
org access token or user API token in the UI.

### New to Observability Cloud? A few terms

- **Realm**: the region your org lives in, such as `eu0` or `us1`. It's part of every API URL.
- **Metric / MTS**: a metric is a named measurement (`cpu.utilization`). An MTS (metric time series) is one
  metric with one combination of dimensions, such as `host=web-1`; a metric has many MTS.
- **SignalFlow**: Observability's query language for metrics, such as
  `data('cpu.utilization').mean(by=['host']).publish()`. A program must end in `.publish()` to produce output.
- **Detector / incident**: a detector is an alert rule; an incident is one firing of it (active until it resolves).
- **APM, service, trace, span**: APM monitors your applications. A service is an application component, a trace is
  one request's path through services, and a span is one step in a trace. Environments (such as `prod`) group services.

### Command reference

Every command takes `-p PROFILE` and, where it prints results, `-f toon|json|csv`, `-o FILE` and `-n LIMIT`.
Times accept `now`, relative values (`-1h`, `-7d`), epoch, or ISO 8601.

| Area | Command | What it does |
|---|---|---|
| Setup | `oy set <realm> [token]` | Save the realm and token on a profile (creates it if needed) |
| Metrics | `oy metrics [query]` | Search metric names, e.g. `'sf_metric:cpu*'` |
| | `oy mts [query]` | Search metric time series metadata (which dimensions exist, and their values) |
| | `oy query '<SignalFlow>'` | Run a SignalFlow program; one row per datapoint (`-e`, `-l`, `-r` for range and resolution) |
| | `oy compare '<SignalFlow>'` | Compare a metric between a `--before` and an `--after` window per group, optionally `--focus` vs a control |
| Alerting | `oy detectors [name]` | Search detectors (alert rules) by name |
| | `oy incidents` | List incidents; `--resolved`, `--detector TEXT`, `--since`, `--until` |
| APM | `oy envs` | List APM environments |
| | `oy services [pattern]` | List APM services, or `--edges` for the dependencies between them |
| | `oy traces <service>` | Find sample traces for a service (`--type error`, `--min-ms`, `--tag k=v`) |
| | `oy trace <traceId>` | Download every span of a trace |
| MCP gateway | `oy tools [name]` | List the tools of Splunk's Observability MCP gateway, or show one tool's fields |
| | `oy tool <name> k=v ...` | Call any gateway tool (latency and error breakdowns, tag autocomplete, detector lookups, ...) |

Typical flows: *what is alerting?* `oy incidents`; *what does this metric look like?* `oy metrics`, then `oy query`;
*did a change help?* `oy compare`; *why was this service slow or failing?* `oy envs`, `oy traces`, `oy trace`,
then `oy tool get_apm_service_latency ...`. Examples of each follow.

```bash
splank -p obs oy set eu0 <token>      # creates the profile if it doesn't exist

splank -p obs oy metrics 'sf_metric:cpu*'
splank -p obs oy mts 'sf_metric:cpu.utilization' -n 20
splank -p obs oy detectors 'high latency'
splank -p obs oy incidents --resolved -f json
splank -p obs oy incidents --resolved --detector latency --since -7d -n 1000

# Before/after comparison of a metric per group, optionally against a control
splank -p obs oy compare "data('request.latency').mean(by=['service']).publish()" \
  --before 2026-10-10 2026-10-23 --after 2026-10-26 2026-11-16 --focus checkout,search

# SignalFlow: actual metric values (program must end in .publish())
splank -p obs oy query "data('cpu.utilization').mean(by=['aws_region']).publish()" -e -1h -r 5m
splank -p obs oy query "data('cpu.utilization', filter=filter('Application','web')).max().publish()" -f csv

# APM: services, their dependencies, and a trace by ID
splank -p obs oy services 'checkout*' --type service
splank -p obs oy services 'checkout*' --edges
splank -p obs oy trace d1a8f3e2d7d3700c -f json

# APM: find sample traces for a service (via the Observability MCP gateway), then fetch one
splank -p obs oy envs
splank -p obs oy traces checkout-service --env prod --type error -e -1h
splank -p obs oy traces checkout-service --env prod --min-ms 500 --tag http.request.method=POST
```

`oy traces` uses the Splunk Observability MCP gateway (the public REST API has no trace search). The gateway URL is
derived from your realm (override with `o11y_mcp_url`), authenticated with the same `o11y_token`, and not available
in Google Cloud or GovCloud realms. It returns a small sample of traces per call (`--type any|error|root-cause`,
`--min-ms`/`--max-ms`, repeated `--tag key=value`) and can time out on very busy services, in which case it warns
and you should narrow `--earliest` or add filters. Pass a returned `traceId` to `oy trace`. Extra MCP toolsets can be
selected with `o11y_mcp_toolsets = "apm,detectors"` on the profile.

### Any Observability MCP tool

Every tool the gateway exposes can be called directly; `splank` reads each tool's input schema from the server, so
new tools and extra toolsets work without code changes.

```bash
splank -p obs oy tools                              # list the tools
splank -p obs oy tools latency                      # filter by name/description
splank -p obs oy tools get_apm_service_latency      # description and input fields of one tool
splank -p obs oy tool get_apm_service_latency environment_name=prod service_name=checkout time_range.start=-1h
splank -p obs oy tool list_mcp_toolsets             # which toolsets exist (apm, rum, logs, detectors, ...)
splank -p obs oy tools --toolsets apm,detectors     # expose more tools for this call
```

Arguments are `key=value`; dotted keys nest (`time_range.start=-1h`) and values are parsed as JSON when possible
(`limit=5`, `flag=true`, `tags_or_dimensions={"http.method":["POST"]}`). Use `--json '{...}'` for a whole object.
The gateway's `params` wrapper and the `o11y_` name prefix are handled for you. Output is TOON by default
(`-f json` / `--raw` for the tool's text unchanged).

`oy incidents` filters: `--detector TEXT` (substring of the detector name), `--since`/`--until` (on the incident's
last state change; only the newest ~10000 incidents are reachable through the API).

`oy compare` runs a SignalFlow program once and compares a statistic (`--stat median|mean|max|min`) of its
datapoints between `--before START END` and `--after START END` per `--group-by` dimension (default: all
dimensions of the series), at `-r` resolution (default 1d), excluding weekends unless `--include-weekends`. A date-only END
includes that day, and a group needs `--min-points` (5) datapoints in both windows. With `--focus a,b` the
groups are labeled focus/control and the median change of each is printed to stderr.

`oy query` takes `-e/--earliest` and `-l/--latest` (`-1h`, `-30m`, `-7d`, `now`, epoch, or ISO 8601) and
`-r/--resolution` (`1m`, `5m`, …). It prints one row per datapoint with `timestamp`, `value`, `label`, `metric`
and the series' dimensions. `oy trace` downloads spans for a known trace ID; the public API has no trace
search, so find IDs from your logs or the UI.

```toml
[profiles.obs]
o11y_realm = "eu0"
o11y_token = "your-observability-token"
# o11y_api_url = "https://api.eu0.observability.splunkcloud.com"   # optional override
```

## MCP

Splunk instances can expose an [MCP](https://modelcontextprotocol.io) server at a URL like
`https://<host>/en-US/splunkd/__raw/services/mcp`. This is separate from the regular management-port
REST API, and typically needs its own bearer token — a plain Splunk API token is usually rejected
because its audience doesn't match what the MCP endpoint expects.

```bash
# Set up a new profile with MCP in one go: derives the MCP URL from the host
# and only prompts for the MCP token
splank init qa splunk-qa.example.com --mcp

# Or add MCP to an existing profile (prompts if url/token omitted)
splank -p qa mcp set https://splunk-qa.example.com/en-US/splunkd/__raw/services/mcp <token>

# List the tools the server exposes
splank -p qa mcp tools
splank -p qa mcp tools --schema   # also show each tool's input schema

# Call a tool (arguments as key=value; values are parsed as JSON when possible)
splank -p qa mcp call splunk_get_info
splank -p qa mcp call splunk_run_query 'query=search index=main | head 5' row_limit=5
```

This adds `mcp_url` and `mcp_token` to the profile's TOML block, alongside its existing `host`/`token`:

```toml
[profiles.qa]
host = "splunk-qa.example.com"
port = 8089
token = "your-token-here"
verify_ssl = true
mcp_url = "https://splunk-qa.example.com/en-US/splunkd/__raw/services/mcp"
mcp_token = "your-mcp-scoped-token"
```

### Search via MCP

`search`, `ask` and `discover` can run their queries through the MCP server instead of the management-port
REST API — useful when port 8089 isn't reachable or the REST token has expired. Either pass `--mcp` for a
single run, or set `mode = "mcp"` on the profile:

```toml
[profiles.qa]
mode = "mcp"                      # search/ask/discover use the MCP server by default
mcp_url = "https://splunk-qa.example.com/en-US/splunkd/__raw/services/mcp"
mcp_token = "your-mcp-scoped-token"
```

```bash
splank -p qa search 'index=main Level=ERROR' --mcp   # one-off
splank -p qa search 'index=main Level=ERROR'         # uses MCP when mode = "mcp"
splank -p qa search 'index=main Level=ERROR' --rest  # force REST on an mcp-mode profile
```

In MCP mode `host`/`token` are not needed. Queries run through the server's `splunk_run_query` tool, so
all `search` options and output formats work, with these differences:

- Results are not streamed; table output appears once the query finishes.
- `--earliest`/`--latest` are sent as tool arguments if the tool accepts them (check with
  `splank mcp tools --schema`); otherwise they are inlined into the SPL (`search earliest=… latest=… …`).
  For queries starting with `|` this isn't possible, so put the time range in the SPL yourself.
- `jobs` and `clear` always use the REST API (MCP has no job handles).
- The MCP server may enforce its own row caps or command restrictions.

### Ask

`ask` uses the MCP server's `saia_generate_spl` tool (Splunk AI Assistant) to turn a free-form
question into SPL, prints the generated query, and then runs it through the same pipeline as
`search` — so all of `search`'s options (`--format`, `--earliest`/`--latest`, `--zoom`, etc.) apply.

This requires the profile's `mcp_url`/`mcp_token` to already be configured (see [MCP](#mcp) above,
`splank mcp set`) — `ask` connects to that same MCP server to generate the SPL.

```bash
splank -p qa ask "show me recent errors in the web_logs index"
splank -p qa ask "find failed logins" --dry-run   # only print the generated SPL, don't run it
```

## Search Options

```bash
splank search 'index=main Level=ERROR' [options]
```

| Option | Description |
|--------|-------------|
| `-e, --earliest` | Earliest time (default: -24h) |
| `-l, --latest` | Latest time (default: now) |
| `-m, --max-results` | Max results (default: 100) |
| `-f, --format` | Output format: json, csv, table, [toon](https://github.com/toon-format/toon-python) (default: toon) |
| `-o, --output` | Output file (default: stdout) |
| `--internal` | Include internal Splunk fields (_bkt, _cd, etc.) |
| `-w, --width` | Truncate field values to N chars (default: 500, 0=no limit) |
| `-z, --zoom` | Parse JSON from _raw and output as toon |

By default, internal Splunk fields (`_bkt`, `_cd`, `_indextime`, `_serial`, `_si`, `_sourcetype`, `_subsecond`) are hidden. Use `--internal` to show them.

The `--zoom` flag is useful when log lines contain JSON - it extracts and parses the JSON from `_raw`, outputs as toon format (compact and human-readable), and ignores Splunk metadata.

## Global Options

- `-p, --profile` - Splunk profile to use (e.g., 'qa', 'prod')
- `-V, --version` - Show version
