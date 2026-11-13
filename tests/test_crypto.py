import pytest
from cryptography.fernet import Fernet, InvalidToken
from app.crypto import TokenCipher


def test_token_roundtrip():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    data = {
        "access_token": "acc_12345",
        "refresh_token": "ref_67890",
        "expires_in": 7200,
        "scope": "tweet.read users.read",
        "obtained_at": "2026-11-13T00:00:00Z",
    }
    encrypted = cipher.encrypt(data)
    assert isinstance(encrypted, bytes)
    assert encrypted != data

    decrypted = cipher.decrypt(encrypted)
    assert decrypted == data


def test_wrong_key_fails():
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()
    cipher1 = TokenCipher(key1)
    cipher2 = TokenCipher(key2)

    data = {"access_token": "secret"}
    encrypted = cipher1.encrypt(data)

    with pytest.raises(InvalidToken):
        cipher2.decrypt(encrypted)

    with pytest.raises(ValueError):
        TokenCipher("")

    with pytest.raises(ValueError):
        TokenCipher("not-a-valid-key")
