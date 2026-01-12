# Splank

CLI tool for querying Splunk logs.

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

## Options

- `-p, --profile` - Splunk profile to use (e.g., 'qa', 'prod')
- `-V, --version` - Show version
