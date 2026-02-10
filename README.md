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

### Configuration

The credentials file supports multiple profiles:

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

# Discover indexes
splank discover 'web*'

# Discover with field info
splank discover 'app-*' --fields -o DISCOVERY.md

# Manage jobs
splank jobs
splank clear
```

## Commands

- `init` - Create credentials file
- `search` - Execute SPL query
- `discover` - Discover available indexes
- `jobs` - List search jobs
- `clear` - Clear my search jobs

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
