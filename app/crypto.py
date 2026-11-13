import json
from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, secret_key: str):
        if not secret_key or not isinstance(secret_key, str):
            raise ValueError("secret_key must be a non-empty string")
        try:
            self._fernet = Fernet(secret_key.encode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid Fernet key: {e}") from e

    def encrypt(self, data: dict) -> bytes:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(raw)

    def decrypt(self, blob: bytes) -> dict:
        decrypted = self._fernet.decrypt(blob)
        return json.loads(decrypted.decode("utf-8"))
