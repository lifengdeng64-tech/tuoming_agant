from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_key(master_key: bytes, purpose: str, tenant_id: str, length: int = 32) -> bytes:
    info = f"tuoming-agent|{purpose}|{tenant_id}".encode()
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master_key)


def encrypt_bytes(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt_bytes(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def serialize_value(value: Any) -> bytes:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        payload = {"type": "bool", "value": value}
    elif isinstance(value, int):
        payload = {"type": "int", "value": value}
    elif isinstance(value, float):
        payload = {"type": "float", "value": value}
    elif isinstance(value, datetime):
        payload = {"type": "datetime", "value": value.isoformat()}
    elif isinstance(value, date):
        payload = {"type": "date", "value": value.isoformat()}
    else:
        payload = {"type": "str", "value": str(value)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def deserialize_value(payload: bytes) -> Any:
    data = json.loads(payload.decode())
    value_type = data["type"]
    value = data["value"]
    if value_type == "bool":
        return bool(value)
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "datetime":
        return datetime.fromisoformat(value)
    if value_type == "date":
        return date.fromisoformat(value)
    return str(value)

