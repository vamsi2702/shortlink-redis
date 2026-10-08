from api.services.codes import ALPHABET, CODE_LENGTH, generate_code


def test_code_has_default_length():
    assert len(generate_code()) == CODE_LENGTH


def test_code_uses_only_base62_characters():
    assert all(c in ALPHABET for c in generate_code(200))


def test_codes_are_not_repeated():
    codes = {generate_code() for _ in range(1000)}

    assert len(codes) == 1000
