from fastapi import Request

from api import config
from api.services.cache import Cache
from api.services.redis_cache import CacheUnavailable
from api.services.security import decode_access_token


def client_identity(request: Request) -> str:
    """Who is asking: the signed-in user if there is a valid token, otherwise the IP address.

    The address is the direct peer of the connection. Behind a proxy that is the proxy, so a real
    deployment must set the proxy up to pass the client on (X-Forwarded-For is NOT trusted here,
    because any client can make that header up).
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        user_id = decode_access_token(token)
        if user_id is not None:
            return f"user:{user_id}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def allow_request(cache: Cache, scope: str, identity: str) -> bool:
    """Count this request and say whether it is within the limit (fixed window).

    The counter is a key that lives for one window. Its first request creates it with a time to
    live (SET NX), every request adds one (INCR, which keeps that time to live), and when the key
    expires the next request starts a new window.

    If the cache is unavailable the request is allowed ("fail open"): a broken cache must not take
    the whole site down with it.
    """
    key = f"ratelimit:{scope}:{identity}"
    window = config.RATE_LIMIT_WINDOW_SECONDS
    try:
        created = cache.set_nx(key, "0", ttl=window)
        count = cache.incr(key)
        if count == 1 and not created:
            # The key expired between the two calls, so INCR made a new one with no time to live.
            # Without this it would never expire and the client would be blocked for ever.
            cache.expire(key, window)
    except CacheUnavailable:
        return True
    return count <= config.RATE_LIMIT_REQUESTS
