"""LM HTTP transport. Resolve localhost to a literal before opening a socket."""
from ipaddress import ip_address
from urllib.parse import urlsplit

import requests


class LocalEndpointError(ValueError):
    pass


def local_base(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme not in ("http", "https") or not host or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or parts.path.rstrip("/") not in ("", "/v1")):
            raise ValueError
        address = ip_address("127.0.0.1" if host == "localhost" else host)
        if not address.is_loopback or "%" in host:
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        port = parts.port
        return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "") + "/v1"
    except (ValueError, TypeError, AttributeError):
        raise LocalEndpointError("Local loopback endpoint required") from None


def request(method, base_url, path, *, timeout, **kwargs):
    base = local_base(base_url).removesuffix("/v1")
    with requests.Session() as session:
        session.trust_env = False
        response = session.request(method, base + path, timeout=timeout, allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError("LM endpoint returned a non-success status")
        return response
