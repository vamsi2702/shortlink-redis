import pytest

from api.services.urls import MAX_URL_LENGTH, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/a/path?x=1&y=2#frag",
        "https://sub.domain.example.co.uk:8443/page",
        "  https://example.com/trimmed  ",
        "https://93.184.216.34/page",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/",
    ],
)
def test_accepts_normal_urls(url):
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://",
        "https://user:pass@example.com",
        "https://exa mple.com",
        "https://example.com/\nsecond-line",
        "http://[::1",
        "https://" + "a" * MAX_URL_LENGTH + ".com",
    ],
)
def test_rejects_malformed_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8000/admin",
        "http://LOCALHOST./x",
        "http://app.localhost",
        "http://printer.local",
        "http://db.internal/x",
        "http://intranet/",
        "http://127.0.0.1",
        "http://127.1",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://0.0.0.0",
        "http://10.0.0.5",
        "http://172.16.0.1",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00::1]/",
    ],
)
def test_rejects_internal_addresses(url):
    with pytest.raises(ValueError):
        validate_url(url)
