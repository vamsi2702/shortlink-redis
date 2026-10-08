import ipaddress
import re
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
# Numeric-looking final label, e.g. "127.1" or "0x7f.0.0.1": browsers read these as IP addresses.
NUMERIC_LABEL = re.compile(r"(0x[0-9a-f]+|\d+)")


def validate_url(url: str) -> str:
    """Return the URL if it is safe to shorten, otherwise raise ValueError with the reason."""
    url = url.strip()
    if not url:
        raise ValueError("URL must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL must be at most {MAX_URL_LENGTH} characters")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("URL must not contain spaces or control characters")

    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError("not a valid URL") from None

    if parts.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not allowed")

    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ValueError("URL must include a host name")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        _check_hostname(host)
        return url

    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise ValueError("private, loopback and reserved IP addresses are not allowed")
    return url


def _check_hostname(host: str) -> None:
    labels = host.split(".")
    if len(labels) < 2 or host.endswith(BLOCKED_SUFFIXES):
        raise ValueError("host must be a public domain name (no localhost or internal names)")
    if NUMERIC_LABEL.fullmatch(labels[-1]):
        raise ValueError("host must be a public domain name, not a numeric address")
