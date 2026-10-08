from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.models import User
from api.services.security import dummy_hash, hash_password, verify_password


class EmailTaken(Exception):
    pass


def create_user(db: Session, email: str, password: str) -> User:
    user = User(email=email.lower(), password_hash=hash_password(password))
    try:
        # same pattern as links: let the unique constraint on `email` settle races
        with db.begin_nested():
            db.add(user)
    except IntegrityError:
        raise EmailTaken from None
    db.commit()
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        verify_password(password, dummy_hash())  # spend the same time as a real check
        return None
    return user if verify_password(password, user.password_hash) else None
