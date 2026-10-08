from sqlalchemy import select

from api.models import Click, Link


def shorten(client, url="https://example.com/page", headers=None):
    return client.post("/shorten", json={"url": url}, headers=headers or {})


def test_shortening_while_logged_in_records_the_owner(client, db, login_as):
    code = shorten(client, headers=login_as("alice@example.com")).json()["code"]

    link = db.scalar(select(Link).where(Link.code == code))

    assert link.user.email == "alice@example.com"


def test_shortening_anonymously_still_works_and_has_no_owner(client, db):
    code = shorten(client).json()["code"]

    assert db.scalar(select(Link).where(Link.code == code)).user_id is None


def test_shortening_with_a_bad_token_is_rejected_not_made_anonymous(client, db):
    response = shorten(client, headers={"Authorization": "Bearer garbage"})

    assert response.status_code == 401


def test_list_requires_login(client):
    assert client.get("/links").status_code == 401


def test_list_shows_only_my_links_newest_first(client, login_as):
    alice, bob = login_as("alice@example.com"), login_as("bob@example.com")
    first = shorten(client, "https://example.com/1", alice).json()["code"]
    second = shorten(client, "https://example.com/2", alice).json()["code"]
    bobs = shorten(client, "https://example.com/bob", bob).json()["code"]
    shorten(client, "https://example.com/anonymous")

    mine = [item["code"] for item in client.get("/links", headers=alice).json()]
    his = [item["code"] for item in client.get("/links", headers=bob).json()]

    assert mine == [second, first]
    assert his == [bobs]


def test_list_paging(client, login_as):
    alice = login_as()
    codes = [shorten(client, f"https://example.com/{i}", alice).json()["code"] for i in range(5)]

    page1 = client.get("/links?limit=2", headers=alice).json()
    page2 = client.get("/links?limit=2&offset=2", headers=alice).json()

    assert [i["code"] for i in page1] == [codes[4], codes[3]]
    assert [i["code"] for i in page2] == [codes[2], codes[1]]
    assert client.get("/links?limit=0", headers=alice).status_code == 422
    assert client.get("/links?limit=101", headers=alice).status_code == 422


def test_owner_can_edit_a_link_and_the_redirect_follows(client, login_as):
    alice = login_as()
    code = shorten(client, "https://example.com/old", alice).json()["code"]

    response = client.patch(
        f"/links/{code}", json={"url": "https://example.com/new"}, headers=alice
    )

    assert response.status_code == 200
    assert response.json()["long_url"] == "https://example.com/new"
    redirect = client.get(f"/{code}", follow_redirects=False)
    assert redirect.headers["location"] == "https://example.com/new"


def test_edit_validates_the_new_url(client, login_as):
    alice = login_as()
    code = shorten(client, headers=alice).json()["code"]

    response = client.patch(f"/links/{code}", json={"url": "http://127.0.0.1/admin"}, headers=alice)

    assert response.status_code == 422


def test_cannot_edit_someone_elses_link(client, db, login_as):
    alice, bob = login_as("alice@example.com"), login_as("bob@example.com")
    code = shorten(client, "https://example.com/alice", alice).json()["code"]

    response = client.patch(f"/links/{code}", json={"url": "https://evil.example.org"}, headers=bob)

    assert response.status_code == 404
    assert db.scalar(select(Link).where(Link.code == code)).long_url == "https://example.com/alice"


def test_cannot_edit_an_anonymous_link(client, login_as):
    code = shorten(client).json()["code"]

    response = client.patch(
        f"/links/{code}", json={"url": "https://example.com/x"}, headers=login_as()
    )

    assert response.status_code == 404


def test_edit_and_delete_require_login(client):
    code = shorten(client).json()["code"]

    assert client.patch(f"/links/{code}", json={"url": "https://example.com"}).status_code == 401
    assert client.delete(f"/links/{code}").status_code == 401


def test_owner_can_delete_a_link_and_it_stops_redirecting(client, login_as):
    alice = login_as()
    code = shorten(client, headers=alice).json()["code"]

    assert client.delete(f"/links/{code}", headers=alice).status_code == 204

    assert client.get(f"/{code}", follow_redirects=False).status_code == 404
    assert client.get("/links", headers=alice).json() == []


def test_deleting_a_link_also_removes_its_clicks(client, db, login_as):
    alice = login_as()
    code = shorten(client, headers=alice).json()["code"]
    link = db.scalar(select(Link).where(Link.code == code))
    db.add_all([Click(link_id=link.id), Click(link_id=link.id)])
    db.flush()

    assert client.delete(f"/links/{code}", headers=alice).status_code == 204

    assert db.scalars(select(Click).where(Click.link_id == link.id)).all() == []


def test_cannot_delete_someone_elses_link(client, login_as):
    alice, bob = login_as("alice@example.com"), login_as("bob@example.com")
    code = shorten(client, headers=alice).json()["code"]

    assert client.delete(f"/links/{code}", headers=bob).status_code == 404

    assert client.get(f"/{code}", follow_redirects=False).status_code == 302


def test_deleting_twice_returns_404_the_second_time(client, login_as):
    alice = login_as()
    code = shorten(client, headers=alice).json()["code"]
    client.delete(f"/links/{code}", headers=alice)

    assert client.delete(f"/links/{code}", headers=alice).status_code == 404
