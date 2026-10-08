def test_short_link_redirects_to_the_long_url(client):
    long_url = "https://example.com/some/long/path?x=1"
    code = client.post("/shorten", json={"url": long_url}).json()["code"]

    response = client.get(f"/{code}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == long_url


def test_unknown_code_returns_404(client):
    response = client.get("/doesnotexist", follow_redirects=False)

    assert response.status_code == 404
    assert response.json() == {"detail": "Short link not found"}


def test_redirect_route_does_not_shadow_other_routes(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
