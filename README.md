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
- `oy` - Query Splunk Observability Cloud / SignalFx (see below)
- `ask` - Ask a free-form question; generates SPL via MCP/SAIA and runs it (see below)

## Observability Cloud (SignalFx)

Splunk Observability Cloud (formerly SignalFx) uses its own API and its own token, separate from the
Splunk `token` and `mcp_token`. Each profile can carry `o11y_realm` and `o11y_token`; a profile may
hold only these (no Splunk `host`). Find the realm in your Observability URL
(`https://app.<realm>.observability.splunkcloud.com`) and create an org access token or user API token
in the UI.

```bash
splank -p obs oy set eu0 <token>      # creates the profile if it doesn't exist

splank -p obs oy metrics 'sf_metric:cpu*'
splank -p obs oy mts 'sf_metric:cpu.utilization' -n 20
splank -p obs oy detectors 'high latency'
splank -p obs oy incidents --resolved -f json

# SignalFlow: actual metric values (program must end in .publish())
splank -p obs oy query "data('cpu.utilization').mean(by=['aws_region']).publish()" -e -1h -r 5m
splank -p obs oy query "data('cpu.utilization', filter=filter('Application','web')).max().publish()" -f csv

# APM: services, their dependencies, and a trace by ID
splank -p obs oy services 'checkout*' --type service
splank -p obs oy services 'checkout*' --edges
splank -p obs oy trace d1a8f3e2d7d3700c -f json
```

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
splank -p qa ask "show me recent errors in web_logs"
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
