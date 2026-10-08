"""Bounded HTTP GET handling with public-address checks at every redirect."""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, Iterator

import requests


class SafeHTTPError(ValueError):
    """Raised when a URL or redirect does not meet the public HTTP policy."""


def _resolve_public_target(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
) -> tuple[list[str], str | None]:
    """Allow only credential-free HTTP(S) URLs with exclusively public IPs.

    DNS lookup failures and empty answers fail closed. Every returned address
    must be globally routable; mixed public/private answers are rejected.
    """
    if not isinstance(url, str) or not url.strip():
        return [], "URL boş olamaz."

    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return [], "Geçersiz URL yapısı."

    if parsed.scheme.lower() not in {"http", "https"}:
        return [], "Yalnızca HTTP/HTTPS protokolleri desteklenmektedir."
    if parsed.username is not None or parsed.password is not None:
        return [], "URL içinde kullanıcı adı veya parola kullanılamaz."

    hostname = parsed.hostname
    if not hostname:
        return [], "Geçersiz domain adı."
    hostname = hostname.rstrip(".")
    if not hostname or "%" in hostname:
        return [], "Geçersiz domain adı."
    if hostname.lower() == "localhost" or hostname.lower().endswith((".localhost", ".local")):
        return [], "Güvenlik riski: Yerel ağ adreslerine erişim engellendi."

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            normalized_host = hostname.encode("idna").decode("ascii")
            lookup = resolver or socket.getaddrinfo
            records = lookup(
                normalized_host,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except Exception as exc:
            return [], f"Domain çözümlenemedi; güvenlik için erişim engellendi ({type(exc).__name__})."

        if not records:
            return [], "Domain için IP adresi bulunamadı; erişim engellendi."
        try:
            addresses = [ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records]
        except (IndexError, TypeError, ValueError):
            return [], "Domain için geçersiz IP yanıtı alındı; erişim engellendi."
    else:
        addresses = [address]

    if not all(address.is_global for address in addresses):
        return [], "Güvenlik riski: Dahili, özel veya ayrılmış IP adreslerine erişim engellendi."
    return list(dict.fromkeys(str(address) for address in addresses)), None


def validate_public_http_url(
    url: str, *, resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
) -> tuple[bool, str | None]:
    addresses, reason = _resolve_public_target(url, resolver=resolver)
    return bool(addresses), reason


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """Connect to a checked IP while retaining the origin's TLS identity."""

    def __init__(self, address: str, hostname: str):
        self.address, self.hostname = address, hostname
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if proxies or verify is not True:
            raise SafeHTTPError("Proxies and disabled TLS verification are not allowed")
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self.address
        if host_params["scheme"] == "https":
            pool_kwargs.update(server_hostname=self.hostname, assert_hostname=self.hostname)
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


def _pinned_get(url: str, **kwargs) -> requests.Response:
    """Use a private session, never proxy/environment DNS or credentials."""
    addresses, reason = _resolve_public_target(url)
    if not addresses:
        raise SafeHTTPError(reason or "No public target")
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname is not None  # Validated by _resolve_public_target.
    hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        host += f":{parsed.port}"
    headers = {key: value for key, value in kwargs.pop("headers", {}).items() if key.lower() != "host"}
    headers["Host"] = host
    session = requests.Session()
    session.trust_env = False
    session.mount(f"{parsed.scheme}://", _PinnedAdapter(addresses[0], hostname))
    try:
        response = session.get(url, headers=headers, **kwargs)
    except BaseException:
        session.close()
        raise
    original_close = response.close

    def close():
        try:
            original_close()
        finally:
            session.close()

    response.close = close  # type: ignore[method-assign]  # Response owns this private session.
    return response


def safe_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 12.0,
    max_redirects: int = 5,
    validator: Callable[[str], tuple[bool, str | None]] | None = None,
    requester: Callable[..., requests.Response] | None = None,
) -> requests.Response:
    """GET a URL, validating each redirect before issuing its request.

    ``validator`` and ``requester`` are injectable for deterministic tests.
    Production requests connect to a validated IP, preserving the hostname
    for HTTP Host, TLS SNI and certificate validation. Environment proxies
    are disabled. Each redirect gets its own checked, pinned connection.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 0 <= max_redirects <= 10:
        raise ValueError("max_redirects must be between 0 and 10")

    validate = validator or validate_public_http_url
    get = requester or _pinned_get
    current_url = url.strip() if isinstance(url, str) else url
    redirects: list[requests.Response] = []
    deadline = time.monotonic() + timeout

    for redirect_number in range(max_redirects + 1):
        valid, reason = validate(current_url)
        if not valid:
            message = reason or "URL güvenlik denetiminden geçemedi."
            if redirect_number:
                message = f"Yönlendirme güvenlik sınırını aştı: {message}"
            raise SafeHTTPError(message)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("Safe GET total timeout expired")
        response = get(
            current_url,
            headers=dict(headers or {}),
            timeout=remaining,
            stream=True,
            allow_redirects=False,
        )
        response_closed = False
        try:
            response_url = getattr(response, "url", None) or current_url
            response_valid, response_reason = validate(response_url)
            if not response_valid:
                raise SafeHTTPError(response_reason or "Yanıt URL'i güvenlik denetiminden geçemedi.")

            location = response.headers.get("Location")
            if response.status_code in {301, 302, 303, 307, 308} and location:
                if redirect_number >= max_redirects:
                    raise SafeHTTPError("Yönlendirme sayısı izin verilen sınırı aştı.")
                next_url = urllib.parse.urljoin(current_url, location)
                redirects.append(response)
                response.close()
                response_closed = True
                current_url = next_url
                continue

            response.url = response_url
            response.history = redirects
            response._safe_http_deadline = deadline  # type: ignore[attr-defined]  # Streaming deadline metadata.
            return response
        except Exception:
            if not response_closed:
                response.close()
            raise

    raise SafeHTTPError("Yönlendirme sayısı izin verilen sınırı aştı.")


def iter_content(response: requests.Response, *, chunk_size: int) -> Iterator[bytes]:
    """Stream a response while enforcing the safe GET's overall deadline."""
    deadline = getattr(response, "_safe_http_deadline", None)
    for chunk in response.iter_content(chunk_size=chunk_size):
        if deadline is not None and time.monotonic() > deadline:
            raise requests.Timeout("Safe GET total timeout expired while reading the response")
        yield chunk
