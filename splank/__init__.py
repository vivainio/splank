"""Splank - CLI tool for querying Splunk logs."""

from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from splank.client import SplunkClient

__version__ = version("splank")


def client(profile: str | None = None) -> "SplunkClient":
    """Get an authenticated Splunk client.

    Returns an authenticated SplunkClient instance using credentials
    from ~/.config/splank/credentials.toml.

    Args:
        profile: Profile name (e.g., 'qa', 'prod'). If None, uses default profile.

    Usage:
        import splank
        client = splank.client()  # uses default profile
        client = splank.client("qa")  # uses qa profile
        results = list(client.search("index=main | head 10"))

    Returns:
        SplunkClient: Authenticated Splunk client
    """
    from splank.config import get_client

    return get_client(profile)
