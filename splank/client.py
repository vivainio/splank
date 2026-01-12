"""Splunk REST API client."""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator


def get_ssl_context(verify: bool = True) -> ssl.SSLContext:
    """Create SSL context."""
    if verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class SplunkClient:
    """Splunk REST API client."""

    def __init__(
        self,
        host: str,
        port: int = 8089,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        verify_ssl: bool = True,
    ):
        self.base_url = f"https://{host}:{port}"
        self.username = username
        self.password = password
        self.token = token
        self.ssl_context = get_ssl_context(verify_ssl)
        self.session_key: str | None = None

    def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Make HTTP request to Splunk API."""
        url = f"{self.base_url}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.session_key:
            headers["Authorization"] = f"Splunk {self.session_key}"

        body = urllib.parse.urlencode(data).encode() if data else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, context=self.ssl_context) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            raise RuntimeError(f"HTTP {e.code}: {error_body}")

    def login(self) -> None:
        """Authenticate and get session key."""
        if self.token:
            return  # Token auth doesn't need login

        if not self.username or not self.password:
            raise ValueError("Username and password required for authentication")

        data = {
            "username": self.username,
            "password": self.password,
            "output_mode": "json",
        }
        result = self._request("POST", "/services/auth/login", data=data)
        self.session_key = result["sessionKey"]

    def search(
        self,
        query: str,
        earliest: str = "-24h",
        latest: str = "now",
        max_results: int = 100,
        stream: bool = False,
    ) -> Iterator[dict]:
        """Execute a search query and return results."""
        # Build search query with head limit to reduce server-side processing
        if query.strip().startswith("|"):
            spl = query
        else:
            spl = f"search {query}"

        # Add head limit if not already present to reduce disk usage
        if "| head" not in spl.lower():
            spl = f"{spl} | head {max_results}"

        data = {
            "search": spl,
            "earliest_time": earliest,
            "latest_time": latest,
            "output_mode": "json",
        }
        result = self._request("POST", "/services/search/jobs", data=data)
        sid = result["sid"]

        if stream:
            yield from self._stream_results(sid, max_results)
        else:
            # Wait for job to complete
            while True:
                status = self._request(
                    "GET",
                    f"/services/search/jobs/{sid}",
                    params={"output_mode": "json"},
                )
                state = status["entry"][0]["content"]["dispatchState"]
                if state == "DONE":
                    break
                if state == "FAILED":
                    raise RuntimeError("Search job failed")
                time.sleep(0.5)

            # Get results
            results = self._request(
                "GET",
                f"/services/search/jobs/{sid}/results",
                params={"output_mode": "json", "count": max_results},
            )
            yield from results.get("results", [])

    def _stream_results(self, sid: str, max_results: int) -> Iterator[dict]:
        """Stream preview results as they become available."""
        seen_count = 0
        while True:
            status = self._request(
                "GET",
                f"/services/search/jobs/{sid}",
                params={"output_mode": "json"},
            )
            state = status["entry"][0]["content"]["dispatchState"]

            # Get preview results
            preview = self._request(
                "GET",
                f"/services/search/jobs/{sid}/results_preview",
                params={
                    "output_mode": "json",
                    "count": max_results,
                    "offset": seen_count,
                },
            )
            new_results = preview.get("results", [])
            for result in new_results:
                yield result
                seen_count += 1
                if seen_count >= max_results:
                    return

            if state == "DONE":
                break
            if state == "FAILED":
                raise RuntimeError("Search job failed")
            time.sleep(0.3)

    def list_jobs(self, count: int = 50) -> list[dict]:
        """List search jobs."""
        result = self._request(
            "GET",
            "/services/search/jobs",
            params={"output_mode": "json", "count": count},
        )
        return result.get("entry", [])

    def delete_job(self, sid: str) -> None:
        """Delete a search job."""
        url = f"{self.base_url}/services/search/jobs/{sid}"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.session_key:
            headers["Authorization"] = f"Splunk {self.session_key}"

        req = urllib.request.Request(url, headers=headers, method="DELETE")
        urllib.request.urlopen(req, context=self.ssl_context)
