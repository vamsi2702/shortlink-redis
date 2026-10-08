import secrets
import string

# base62: a-z, A-Z, 0-9
ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 6


def generate_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
