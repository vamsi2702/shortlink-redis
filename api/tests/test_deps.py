from sqlalchemy import text
from sqlalchemy.orm import Session

from api.deps import get_cache, get_db
from api.services.cache import default_cache


def test_get_db_yields_a_working_session_and_closes_it():
    generator = get_db()
    session = next(generator)

    assert isinstance(session, Session)
    assert session.execute(text("select 1")).scalar() == 1
    generator.close()


def test_get_cache_returns_the_shared_app_cache():
    assert get_cache() is default_cache
