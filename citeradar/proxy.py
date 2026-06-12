"""Proxy configuration and rotating Requests sessions."""

import random
from typing import Optional

import requests
import yaml

_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


class ProxyConfigError(ValueError):
    """Raised when a proxy configuration file is invalid."""


def _normalise_url(url: str) -> str:
    if not isinstance(url, str):
        raise ProxyConfigError("Proxy URLs must be strings.")
    url = str(url).strip()
    if not url:
        raise ProxyConfigError("Proxy URLs must be non-empty strings.")
    if "://" in url:
        return url
    if ":" in url and url.split(":", 1)[0].lower() in _PROXY_SCHEMES:
        scheme, rest = url.split(":", 1)
        return f"{scheme}://{rest}"
    return f"http://{url}"


def _normalise_endpoint(raw: dict, proxy_id: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProxyConfigError(f"Proxy group '{proxy_id}' entries must be mappings.")

    endpoint = {
        scheme: _normalise_url(url)
        for scheme, url in raw.items()
        if scheme in {"http", "https"} and url
    }
    if not endpoint:
        raise ProxyConfigError(
            f"Proxy group '{proxy_id}' entries must define http or https."
        )
    if "http" not in endpoint:
        endpoint["http"] = endpoint["https"]
    if "https" not in endpoint:
        endpoint["https"] = endpoint["http"]
    return endpoint


class ProxyPool:
    """Round-robin proxy IDs, randomly choosing an endpoint within each ID."""

    def __init__(self, groups: list[tuple[str, list[dict[str, str]]]]) -> None:
        if not groups:
            raise ProxyConfigError("Proxy configuration must contain at least one group.")
        self.groups = groups
        self._index = 0

    def next(self) -> dict[str, str]:
        proxy_id, endpoints = self.groups[self._index]
        self._index = (self._index + 1) % len(self.groups)
        endpoint = random.choice(endpoints)
        return dict(endpoint)


class RotatingProxySession(requests.Session):
    """Requests session that injects a rotated proxy into each request."""

    def __init__(self, proxy_pool: ProxyPool) -> None:
        super().__init__()
        self.proxy_pool = proxy_pool

    def request(self, method, url, **kwargs):
        if kwargs.get("proxies") is None:
            kwargs["proxies"] = self.proxy_pool.next()
        return super().request(method, url, **kwargs)


def load_proxy_pool(path: str) -> ProxyPool:
    """Load a proxy pool from a YAML configuration file."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ProxyConfigError(f"Could not read proxy config '{path}': {e}") from e
    except yaml.YAMLError as e:
        raise ProxyConfigError(f"Invalid proxy config YAML '{path}': {e}") from e

    if not isinstance(raw, dict) or not raw:
        raise ProxyConfigError("Proxy config must be a non-empty mapping.")

    groups = []
    for proxy_id, entries in raw.items():
        if not isinstance(entries, list) or not entries:
            raise ProxyConfigError(
                f"Proxy group '{proxy_id}' must be a non-empty list."
            )
        endpoints = [_normalise_endpoint(entry, str(proxy_id)) for entry in entries]
        groups.append((str(proxy_id), endpoints))
    return ProxyPool(groups)


def make_session(proxy_pool: Optional[ProxyPool] = None) -> requests.Session:
    """Create a Requests session, optionally backed by rotating proxies."""
    if proxy_pool is None:
        return requests.Session()
    return RotatingProxySession(proxy_pool)
