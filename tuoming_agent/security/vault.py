from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterator
from typing import Any

from tuoming_agent.models import utc_now
from tuoming_agent.security.crypto import (
    decrypt_bytes,
    derive_key,
    deserialize_value,
    encrypt_bytes,
    serialize_value,
)
from tuoming_agent.security.normalization import normalize_domain, normalize_value
from tuoming_agent.storage.base import Repository
from tuoming_agent.storage.errors import RecordNotFoundError


class TokenCollisionError(RuntimeError):
    """Raised when all deterministic collision expansion options are exhausted."""


class TokenVault:
    """Deterministic HMAC tokenization backed by an encrypted, tenant-scoped vault."""

    def __init__(self, repository: Repository, master_key: bytes, key_version: int = 1):
        if len(master_key) < 32:
            raise ValueError("master_key must contain at least 32 bytes.")
        if key_version < 1:
            raise ValueError("key_version must be positive.")
        self.repository = repository
        self.master_key = master_key
        self.key_version = key_version

    def tokenize(
        self,
        tenant_id: str,
        domain: str,
        value: Any,
        normalizer: str = "text",
        key_version: int | None = None,
    ) -> str:
        self.repository.ensure_tenant(tenant_id)
        version = key_version or self.key_version
        safe_domain = normalize_domain(domain)
        normalized = normalize_value(value, normalizer)
        payload = json.dumps(
            {
                "tenant_scope": tenant_id,
                "masking_domain": safe_domain,
                "normalized_value": normalized,
                "key_version": version,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        token_key = derive_key(self.master_key, "token", tenant_id)
        digest = hmac.new(token_key, payload, hashlib.sha256).digest()
        fingerprint = digest.hex()

        existing = self.repository.find_mapping_by_fingerprint(
            tenant_id, safe_domain, version, fingerprint
        )
        if existing:
            return str(existing["token"])

        for byte_length in (10, 12, 16, 20, 24, 28, 32):
            token = self._candidate_token(safe_domain, version, digest, byte_length)
            collision = self.repository.find_mapping_by_token(tenant_id, token)
            if collision and collision["fingerprint"] != fingerprint:
                continue
            nonce, encrypted = self._encrypt_value(tenant_id, safe_domain, version, token, value)
            try:
                self.repository.insert_mapping(
                    {
                        "tenant_id": tenant_id,
                        "domain": safe_domain,
                        "key_version": version,
                        "fingerprint": fingerprint,
                        "token": token,
                        "encrypted_value": encrypted,
                        "nonce": nonce,
                        "created_at": utc_now(),
                    }
                )
                return token
            except sqlite3.IntegrityError:
                concurrent = self.repository.find_mapping_by_fingerprint(
                    tenant_id, safe_domain, version, fingerprint
                )
                if concurrent:
                    return str(concurrent["token"])
        raise TokenCollisionError("Unable to create a unique deterministic token.")

    def tokenize_many(
        self,
        tenant_id: str,
        domain: str,
        values: list[Any],
        normalizer: str = "text",
        key_version: int | None = None,
    ) -> list[str]:
        """Tokenize a batch using one SQLite transaction and one tenant check."""
        if not values:
            return []
        self.repository.ensure_tenant(tenant_id)
        connect = getattr(self.repository, "_connect", None)
        if connect is None:
            return [
                self.tokenize(tenant_id, domain, value, normalizer, key_version)
                for value in values
            ]

        version = key_version or self.key_version
        safe_domain = normalize_domain(domain)
        token_key = derive_key(self.master_key, "token", tenant_id)
        prepared: list[str] = []
        unique: dict[str, tuple[bytes, Any]] = {}
        for value in values:
            normalized = normalize_value(value, normalizer)
            payload = json.dumps(
                {
                    "tenant_scope": tenant_id,
                    "masking_domain": safe_domain,
                    "normalized_value": normalized,
                    "key_version": version,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            digest = hmac.new(token_key, payload, hashlib.sha256).digest()
            fingerprint = digest.hex()
            prepared.append(fingerprint)
            unique.setdefault(fingerprint, (digest, value))

        tokens: dict[str, str] = {}
        with connect() as connection:
            fingerprints = list(unique)
            for offset in range(0, len(fingerprints), 900):
                batch = fingerprints[offset : offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""SELECT fingerprint, token FROM token_mappings
                    WHERE tenant_id = ? AND domain = ? AND key_version = ?
                    AND fingerprint IN ({placeholders})""",
                    (tenant_id, safe_domain, version, *batch),
                ).fetchall()
                tokens.update(
                    {str(row["fingerprint"]): str(row["token"]) for row in rows}
                )

            for fingerprint, (digest, value) in unique.items():
                if fingerprint in tokens:
                    continue
                for byte_length in (10, 12, 16, 20, 24, 28, 32):
                    token = self._candidate_token(safe_domain, version, digest, byte_length)
                    nonce, encrypted = self._encrypt_value(
                        tenant_id, safe_domain, version, token, value
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO token_mappings(
                            tenant_id, domain, key_version, fingerprint, token,
                            encrypted_value, nonce, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tenant_id,
                            safe_domain,
                            version,
                            fingerprint,
                            token,
                            encrypted,
                            nonce,
                            utc_now(),
                        ),
                    )
                    row = connection.execute(
                        """SELECT token FROM token_mappings
                        WHERE tenant_id = ? AND domain = ?
                        AND key_version = ? AND fingerprint = ?""",
                        (tenant_id, safe_domain, version, fingerprint),
                    ).fetchone()
                    if row:
                        tokens[fingerprint] = str(row["token"])
                        break
                else:
                    raise TokenCollisionError("Unable to create a unique deterministic token.")
        return [tokens[fingerprint] for fingerprint in prepared]

    def resolve(self, tenant_id: str, token: str) -> Any:
        mapping = self.repository.find_mapping_by_token(tenant_id, token)
        if mapping is None:
            # Check whether the token exists elsewhere without disclosing its owner.
            raise RecordNotFoundError("Token not found for this tenant.")
        return self._decrypt_mapping(mapping)

    def resolve_many(self, tenant_id: str, tokens: list[str]) -> list[Any]:
        """Resolve one bounded token batch with batched SQLite reads."""
        if not tokens:
            return []
        connect = getattr(self.repository, "_connect", None)
        if connect is None:
            return [self.resolve(tenant_id, token) for token in tokens]
        unique = list(dict.fromkeys(tokens))
        mappings: dict[str, dict[str, Any]] = {}
        with connect() as connection:
            for offset in range(0, len(unique), 900):
                batch = unique[offset : offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""SELECT * FROM token_mappings
                    WHERE tenant_id = ? AND token IN ({placeholders})""",
                    (tenant_id, *batch),
                ).fetchall()
                mappings.update({str(row["token"]): dict(row) for row in rows})
        missing = next((token for token in unique if token not in mappings), None)
        if missing is not None:
            raise RecordNotFoundError("Token not found for this tenant.")
        resolved = {token: self._decrypt_mapping(mappings[token]) for token in unique}
        return [resolved[token] for token in tokens]

    def iter_plaintext_tokens(self, tenant_id: str) -> Iterator[tuple[str, str]]:
        for mapping in self.repository.list_mappings(tenant_id):
            value = self._decrypt_mapping(mapping)
            if isinstance(value, str) and value:
                yield value, str(mapping["token"])

    def _encrypt_value(
        self, tenant_id: str, domain: str, version: int, token: str, value: Any
    ) -> tuple[bytes, bytes]:
        encryption_key = derive_key(self.master_key, "vault", tenant_id)
        aad = self._aad(tenant_id, domain, version, token)
        return encrypt_bytes(encryption_key, serialize_value(value), aad)

    def _decrypt_mapping(self, mapping: dict[str, Any]) -> Any:
        tenant_id = str(mapping["tenant_id"])
        encryption_key = derive_key(self.master_key, "vault", tenant_id)
        aad = self._aad(
            tenant_id,
            str(mapping["domain"]),
            int(mapping["key_version"]),
            str(mapping["token"]),
        )
        plaintext = decrypt_bytes(
            encryption_key,
            bytes(mapping["nonce"]),
            bytes(mapping["encrypted_value"]),
            aad,
        )
        return deserialize_value(plaintext)

    @staticmethod
    def _candidate_token(domain: str, version: int, digest: bytes, byte_length: int) -> str:
        fragment = base64.b32encode(digest[:byte_length]).decode("ascii").rstrip("=")
        return f"{domain}_V{version}_{fragment}"

    @staticmethod
    def _aad(tenant_id: str, domain: str, version: int, token: str) -> bytes:
        return f"{tenant_id}|{domain}|{version}|{token}".encode()
