from api.routes.frontend import STATIC_DIR


def test_home_page_is_served_as_html(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>ShortLink</title>" in response.text


def test_home_page_is_not_listed_as_an_api_endpoint(client):
    assert "/" not in client.get("/openapi.json").json()["paths"]


def test_home_page_does_not_break_short_link_redirects(client):
    code = client.post("/shorten", json={"url": "https://example.com/x"}).json()["code"]

    assert client.get(f"/{code}", follow_redirects=False).status_code == 302


def test_page_never_inserts_server_text_as_html():
    """Guard against XSS: user-supplied URLs must only be inserted with textContent."""
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    assert "document.write" not in page
