from __future__ import annotations

import json
import os
import struct
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, BinaryIO

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STREAM_MAGIC = b"TUOMING\x01"
_FRAME_HEADER = struct.Struct(">BQI")
_FINAL_FRAME = 1


def derive_key(master_key: bytes, purpose: str, tenant_id: str, length: int = 32) -> bytes:
    info = f"tuoming-agent|{purpose}|{tenant_id}".encode()
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master_key)


def encrypt_bytes(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt_bytes(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def encrypt_stream_frames(
    key: bytes,
    source: BinaryIO,
    target: BinaryIO,
    aad: bytes,
    chunk_size: int,
) -> tuple[str, int]:
    """Encrypt bounded source reads as independently authenticated AES-GCM frames."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive.")
    import hashlib

    digest = hashlib.sha256()
    total_size = 0
    frame_index = 0
    target.write(STREAM_MAGIC)
    aesgcm = AESGCM(key)
    while True:
        plaintext = source.read(chunk_size)
        if not plaintext:
            break
        digest.update(plaintext)
        total_size += len(plaintext)
        nonce = os.urandom(12)
        header = _FRAME_HEADER.pack(0, frame_index, len(plaintext))
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad + header)
        target.write(header)
        target.write(nonce)
        target.write(ciphertext)
        frame_index += 1

    final_plaintext = digest.digest() + struct.pack(">Q", total_size)
    nonce = os.urandom(12)
    header = _FRAME_HEADER.pack(_FINAL_FRAME, frame_index, len(final_plaintext))
    target.write(header)
    target.write(nonce)
    target.write(aesgcm.encrypt(nonce, final_plaintext, aad + header))
    return digest.hexdigest(), total_size


def decrypt_stream_frames(key: bytes, source: BinaryIO, aad: bytes) -> Iterator[bytes]:
    """Yield authenticated plaintext frames and reject truncation or reordering."""
    import hashlib

    digest = hashlib.sha256()
    total_size = 0
    expected_index = 0
    aesgcm = AESGCM(key)
    while True:
        header = source.read(_FRAME_HEADER.size)
        if len(header) != _FRAME_HEADER.size:
            raise ValueError("Encrypted stream is missing its authenticated final frame.")
        frame_type, frame_index, plaintext_size = _FRAME_HEADER.unpack(header)
        if frame_index != expected_index or frame_type not in (0, _FINAL_FRAME):
            raise ValueError("Encrypted stream frame sequence is invalid.")
        nonce = source.read(12)
        ciphertext = source.read(plaintext_size + 16)
        if len(nonce) != 12 or len(ciphertext) != plaintext_size + 16:
            raise ValueError("Encrypted stream frame is truncated.")
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad + header)
        if frame_type == _FINAL_FRAME:
            if plaintext_size != 40:
                raise ValueError("Encrypted stream final frame is invalid.")
            expected_digest, expected_size = plaintext[:32], struct.unpack(">Q", plaintext[32:])[0]
            if digest.digest() != expected_digest or total_size != expected_size:
                raise ValueError("Encrypted stream integrity metadata does not match.")
            if source.read(1):
                raise ValueError("Encrypted stream contains data after its final frame.")
            return
        digest.update(plaintext)
        total_size += len(plaintext)
        expected_index += 1
        yield plaintext


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
