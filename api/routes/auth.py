from fastapi import APIRouter, HTTPException

from api.deps import CurrentUser, DbSession, rate_limited
from api.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from api.services.security import create_access_token
from api.services.users import EmailTaken, authenticate, create_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=UserOut, status_code=201, dependencies=[rate_limited("auth")]
)
def register(body: RegisterRequest, db: DbSession):
    try:
        return create_user(db, body.email, body.password)
    except EmailTaken:
        raise HTTPException(status_code=409, detail="Email already registered") from None


@router.post("/login", response_model=TokenResponse, dependencies=[rate_limited("auth")])
def login(body: LoginRequest, db: DbSession) -> TokenResponse:
    user = authenticate(db, body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user
