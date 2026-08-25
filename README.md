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

## MCP

Splunk instances can expose an [MCP](https://modelcontextprotocol.io) server at a URL like
`https://<host>/en-US/splunkd/__raw/services/mcp`. This is separate from the regular management-port
REST API, and typically needs its own bearer token — a plain Splunk API token is usually rejected
because its audience doesn't match what the MCP endpoint expects.

```bash
# Save the MCP URL and token for a profile (prompts if omitted)
splank -p qa mcp set https://splunk-qa.example.com/en-US/splunkd/__raw/services/mcp <token>

# List the tools the server exposes
splank -p qa mcp tools

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
