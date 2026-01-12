"""Configuration handling for Splank."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from platformdirs import user_config_dir

from splank.client import SplunkClient

CONFIG_DIR = Path(user_config_dir("splank"))
CREDENTIALS_FILE = CONFIG_DIR / "credentials.toml"


def load_credentials() -> dict:
    """Load credentials from $XDG_CONFIG_HOME/splank/credentials.toml."""
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


def get_client(profile: str | None = None) -> SplunkClient:
    """Load credentials and create authenticated client.

    Args:
        profile: Profile name (e.g., 'qa', 'prod'). If None, uses default profile.
    """
    creds = get_profile(profile)

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
