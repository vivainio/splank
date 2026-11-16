"""Configuration handling for Splank."""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from platformdirs import user_config_dir

from splank.client import SplunkClient
from splank.mcp_client import MCPClient
from splank.o11y_client import O11yClient, api_url_for_realm

CONFIG_DIR = Path(user_config_dir("splank", appauthor=False))
CREDENTIALS_FILE = CONFIG_DIR / "credentials.toml"

# Old path used before the appauthor=False fix (Windows had an extra splank\ level)
_LEGACY_CREDENTIALS_FILE = Path(user_config_dir("splank")) / "credentials.toml"


def _migrate_legacy_credentials() -> None:
    if not CREDENTIALS_FILE.exists() and _LEGACY_CREDENTIALS_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_CREDENTIALS_FILE, CREDENTIALS_FILE)
        print(f"Migrated credentials from {_LEGACY_CREDENTIALS_FILE} to {CREDENTIALS_FILE}", file=sys.stderr)


def load_credentials() -> dict:
    """Load credentials from $XDG_CONFIG_HOME/splank/credentials.toml."""
    _migrate_legacy_credentials()
    if not CREDENTIALS_FILE.exists():
        return {}

    with open(CREDENTIALS_FILE, "rb") as f:
        return tomllib.load(f)


def get_profile(profile: str | None = None) -> dict:
    """Get credentials for a specific profile.

    Args:
        profile: Profile name (e.g., 'qa', 'prod'). If None, uses 'default' profile.

    Returns:
        Profile configuration dict with host, port, username, password, token, verify_ssl.
    """
    creds = load_credentials()

    if not creds:
        print(
            f"Credentials not configured in {CREDENTIALS_FILE}",
            file=sys.stderr,
        )
        print("\nRun 'splank init' to set up credentials.", file=sys.stderr)
        sys.exit(1)

    # Determine which profile to use
    profile_name = profile or creds.get("default_profile", "default")

    # Get profile from profiles section
    profiles = creds.get("profiles", {})
    if profile_name in profiles:
        return profiles[profile_name]

    # Fall back to top-level config (for simple single-profile setup)
    if "host" in creds:
        return creds

    print(f"Profile '{profile_name}' not found in {CREDENTIALS_FILE}", file=sys.stderr)
    print(f"Available profiles: {', '.join(profiles.keys()) or '(none)'}", file=sys.stderr)
    sys.exit(1)


def get_client(
    profile: str | None = None, mcp: bool | None = None
) -> SplunkClient | MCPClient:
    """Load credentials and create a client able to run searches.

    Args:
        profile: Profile name (e.g., 'qa', 'prod'). If None, uses default profile.
        mcp: True to search through the profile's MCP server, False to force the
            REST API, None (default) to follow the profile's ``mode`` key
            (``mode = "mcp"`` selects MCP, anything else selects REST).
    """
    creds = get_profile(profile)

    if mcp is None:
        mcp = str(creds.get("mode", "rest")).lower() == "mcp"
    if mcp:
        return get_mcp_client(profile)

    host = creds.get("host")
    if not host:
        print(f"'host' is required in profile", file=sys.stderr)
        sys.exit(1)

    client = SplunkClient(
        host=host,
        port=creds.get("port", 8089),
        username=creds.get("username"),
        password=creds.get("password"),
        token=creds.get("token"),
        verify_ssl=creds.get("verify_ssl", True),
    )
    client.login()
    return client


def get_mcp_client(profile: str | None = None) -> MCPClient:
    """Load credentials and create an MCP client for the profile's MCP server.

    Args:
        profile: Profile name (e.g., 'qa', 'prod'). If None, uses default profile.
    """
    creds = get_profile(profile)

    mcp_url = creds.get("mcp_url")
    if not mcp_url:
        print(
            "'mcp_url' is not set for this profile.",
            file=sys.stderr,
        )
        print(
            "Run 'splank mcp set <url> <token>' to configure it.", file=sys.stderr
        )
        sys.exit(1)

    return MCPClient(
        url=mcp_url,
        token=creds.get("mcp_token"),
        verify_ssl=creds.get("verify_ssl", True),
    )


def get_o11y_client(profile: str | None = None) -> O11yClient:
    """Load credentials and create a Splunk Observability Cloud (SignalFx) client.

    Uses ``o11y_token`` plus either ``o11y_realm`` (e.g. 'eu0') or an explicit
    ``o11y_api_url`` override from the profile.
    """
    creds = get_profile(profile)

    token = creds.get("o11y_token")
    api_url = creds.get("o11y_api_url")
    realm = creds.get("o11y_realm")
    if not token or not (api_url or realm):
        print("'o11y_realm' and 'o11y_token' are not set for this profile.", file=sys.stderr)
        print("Run 'splank oy set <realm> <token>' to configure them.", file=sys.stderr)
        sys.exit(1)

    return O11yClient(
        api_url=api_url or api_url_for_realm(realm),
        token=token,
        verify_ssl=creds.get("verify_ssl", True),
    )


def init_config() -> None:
    """Initialize credentials file with example values and open in editor."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    example = """\
# Splank configuration
# Credentials are stored in TOML format

# Default profile to use when --profile is not specified
default_profile = "prod"

[profiles.prod]
host = "splunk-prod.example.com"
port = 8089
# Use either username/password or token authentication
username = ""
password = ""
token = ""
verify_ssl = true

[profiles.qa]
host = "splunk-qa.example.com"
port = 8089
username = ""
password = ""
token = ""
verify_ssl = true
"""
    if CREDENTIALS_FILE.exists():
        print(f"Credentials file already exists: {CREDENTIALS_FILE}")
        print("Edit it directly or delete it first to reinitialize.")
        return

    CREDENTIALS_FILE.write_text(example)
    CREDENTIALS_FILE.chmod(0o600)
    print(f"Credentials file created: {CREDENTIALS_FILE}")

    editor = (
        os.environ.get("EDITOR")
        or shutil.which("nano")
        or shutil.which("vim")
        or shutil.which("vi")
    )
    if editor:
        subprocess.run([editor, str(CREDENTIALS_FILE)])
    else:
        print("No editor found. Please edit the credentials file manually.")


def _toml_str(value: str) -> str:
    """Format a string as a quoted TOML basic string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _expand_host(host: str) -> str:
    """Expand a short Splunk Cloud stack name to its full hostname.

    A bare stack name like 'mystack' has no dot, so it's assumed to be a
    Splunk Cloud stack and expanded to '<stack>.splunkcloud.com'. Anything
    with a dot already (or an IP) is left as-is.
    """
    host = host.strip()
    if "." not in host:
        return f"{host}.splunkcloud.com"
    return host


def _format_profile_block(name: str, cfg: dict) -> str:
    lines = [
        f"[profiles.{name}]",
        f"host = {_toml_str(cfg['host'])}",
        f"port = {cfg['port']}",
        f"token = {_toml_str(cfg['token'])}",
        f"verify_ssl = {'true' if cfg['verify_ssl'] else 'false'}",
    ]
    return "\n".join(lines) + "\n"


def _remove_profile_block(text: str, name: str) -> str:
    """Strip an existing [profiles.<name>] table (and its keys) from raw TOML text."""
    pattern = re.compile(
        rf"^\[profiles\.{re.escape(name)}\]\n(?:(?!^\[).*\n?)*",
        re.MULTILINE,
    )
    return pattern.sub("", text)


def _set_default_profile(name: str) -> None:
    text = CREDENTIALS_FILE.read_text()
    if re.search(r"^default_profile\s*=", text, re.MULTILINE):
        text = re.sub(
            r"^default_profile\s*=.*$",
            f'default_profile = "{name}"',
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = f'default_profile = "{name}"\n\n' + text
    CREDENTIALS_FILE.write_text(text)


def resolve_profile_name(profile: str | None = None) -> str:
    """Resolve the profile name to operate on: given name, else default_profile."""
    creds = load_credentials()
    name = profile or creds.get("default_profile")
    if not name:
        print("No profile specified and no default_profile configured.", file=sys.stderr)
        print("Specify one with -p/--profile.", file=sys.stderr)
        sys.exit(1)
    return name


def _set_profile_keys(name: str, values: dict[str, str]) -> None:
    """Add or update string keys of an existing profile in place.

    Preserves every other key already in the profile's TOML block.
    """
    if not CREDENTIALS_FILE.exists():
        print(f"No credentials file at {CREDENTIALS_FILE}. Run 'splank init' first.", file=sys.stderr)
        sys.exit(1)

    creds = load_credentials()
    profiles = creds.get("profiles", {})
    if name not in profiles:
        print(f"Profile '{name}' not found in {CREDENTIALS_FILE}", file=sys.stderr)
        print(f"Available profiles: {', '.join(profiles.keys()) or '(none)'}", file=sys.stderr)
        sys.exit(1)

    text = CREDENTIALS_FILE.read_text()
    pattern = re.compile(
        rf"^\[profiles\.{re.escape(name)}\]\n((?:(?!^\[).*\n?)*)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        print(f"Could not locate [profiles.{name}] block in {CREDENTIALS_FILE}", file=sys.stderr)
        sys.exit(1)

    block_body = match.group(1)
    for key in values:
        block_body = re.sub(rf"^{re.escape(key)}\s*=.*\n?", "", block_body, flags=re.MULTILINE)
    block_body = block_body.rstrip("\n") + "\n"
    for key, value in values.items():
        block_body += f"{key} = {_toml_str(value)}\n"

    new_block = f"[profiles.{name}]\n{block_body}"
    text = text[: match.start()] + new_block + text[match.end():]
    CREDENTIALS_FILE.write_text(text)
    CREDENTIALS_FILE.chmod(0o600)


def set_mcp_config(name: str, mcp_url: str, mcp_token: str) -> None:
    """Add or update the mcp_url/mcp_token keys of an existing profile in place."""
    _set_profile_keys(name, {"mcp_url": mcp_url, "mcp_token": mcp_token})


def set_o11y_config(name: str, realm: str, token: str) -> None:
    """Add or update the o11y_realm/o11y_token keys of a profile.

    The Observability token is separate from the Splunk ``token``. If the
    profile doesn't exist yet, an Observability-only profile (no Splunk host)
    is created.
    """
    if name not in load_credentials().get("profiles", {}):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        block = (
            f"[profiles.{name}]\n"
            f"o11y_realm = {_toml_str(realm)}\n"
            f"o11y_token = {_toml_str(token)}\n"
        )
        if CREDENTIALS_FILE.exists():
            CREDENTIALS_FILE.write_text(CREDENTIALS_FILE.read_text().rstrip() + "\n\n" + block)
        else:
            CREDENTIALS_FILE.write_text(f'default_profile = "{name}"\n\n' + block)
        CREDENTIALS_FILE.chmod(0o600)
        return
    _set_profile_keys(name, {"o11y_realm": realm, "o11y_token": token})


def add_profile_wizard(name: str | None = None, host: str | None = None, mcp: bool = False) -> None:
    """Interactively add (or replace) a single profile in the credentials file.

    Args:
        name: Profile name (e.g. 'qa'). Prompted for if not given.
        host: Splunk host. A bare stack name (no dot) is expanded to
            '<name>.splunkcloud.com'. Prompted for if not given.
        mcp: If True, also derive the MCP server URL from the host and
            prompt for an MCP token, saving both to the profile.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    creds = load_credentials()
    existing_profiles = set(creds.get("profiles", {}).keys())

    if not name:
        name = input("Profile name (e.g. qa, prod): ").strip()
    if not name:
        print("Profile name is required.", file=sys.stderr)
        sys.exit(1)

    if name in existing_profiles:
        overwrite = input(f"Profile '{name}' already exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("Aborted.")
            return

    if not host:
        host = input("Splunk host (e.g. splunk.example.com, or a bare Splunk Cloud stack name): ").strip()
    if not host:
        print("Host is required.", file=sys.stderr)
        sys.exit(1)
    host = _expand_host(host)

    port = 8089

    token = input("Token: ").strip()
    if not token:
        print("Token is required.", file=sys.stderr)
        sys.exit(1)

    verify_ssl = True

    profile_cfg = {
        "host": host,
        "port": port,
        "token": token,
        "verify_ssl": verify_ssl,
    }

    print(f"\nTesting connection to {host}:{port}...")
    try:
        client = SplunkClient(
            host=host,
            port=port,
            token=token,
            verify_ssl=verify_ssl,
        )
        client.login()
        client.list_jobs(count=1)
        print("Connection OK.")
    except Exception as e:
        print(f"Warning: could not verify connection ({e})", file=sys.stderr)
        proceed = input("Save profile anyway? [y/N]: ").strip().lower()
        if proceed != "y":
            print("Aborted.")
            return

    block = _format_profile_block(name, profile_cfg)

    if not CREDENTIALS_FILE.exists():
        header = f'# Splank configuration\n\ndefault_profile = "{name}"\n\n'
        CREDENTIALS_FILE.write_text(header + block)
        CREDENTIALS_FILE.chmod(0o600)
    else:
        text = CREDENTIALS_FILE.read_text()
        if name in existing_profiles:
            text = _remove_profile_block(text, name)
        text = text.rstrip() + "\n\n" + block
        CREDENTIALS_FILE.write_text(text)
        CREDENTIALS_FILE.chmod(0o600)

    if name not in existing_profiles or "default_profile" not in creds:
        make_default_prompt = True
    else:
        make_default_prompt = creds.get("default_profile") != name

    if make_default_prompt:
        make_default = input(f"Set '{name}' as default profile? [y/N]: ").strip().lower()
        if make_default == "y":
            _set_default_profile(name)

    print(f"Profile '{name}' saved to {CREDENTIALS_FILE}")

    if mcp:
        mcp_url = f"https://{host}/en-US/splunkd/__raw/services/mcp"
        mcp_token = input(f"MCP bearer token for {mcp_url}: ").strip()
        if not mcp_token:
            print("No MCP token given; skipping MCP setup.", file=sys.stderr)
            return
        set_mcp_config(name, mcp_url, mcp_token)
        print(f"Saved mcp_url/mcp_token for profile '{name}'.")
