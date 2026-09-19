"""Encrypted secret backup: AES-256-GCM envelope for ``secrets/secrets.enc``.

Bootstrap design (two layers, no circular dependency):
1. BOOTSTRAP credential (env ``B2_BOOTSTRAP_*``) — read-only, prefix-scoped by policy
   (documented: B2 app key, bucket=state bucket, capabilities=[readFiles],
   namePrefix="secrets/") — can ONLY fetch the encrypted blob. Never stored in the blob.
2. MASTER key (env ``SECRETS_MASTER_KEY``) — 32 bytes as 64 hex chars (or a passphrase
   stretched with scrypt when ``SECRETS_MASTER_KEY_TYPE=passphrase``). Lives ONLY in the
   runtime secret environment. Never inside the bucket, never in git.

Envelope (JSON, unambiguous + versioned):
    {"v": 1, "alg": "AES-256-GCM", "kdf": "none"|"scrypt", "salt": b64?,
     "nonce": b64, "ct": b64}
AAD: b"hermes-persist/secrets.enc/v1" — binds ciphertext to purpose+version.
Unique 96-bit nonce per encryption. Authentication failure → fail-closed (exception).

Dependency: the well-established ``cryptography`` package (AESGCM). Imported lazily with
a clear error so stdlib-only ops (backup/restore) keep working without it.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Dict, Optional

FORMAT_VERSION = 1
MAGIC_AAD = b"hermes-persist/secrets.enc/v1"
KEY_HEX_LEN = 64  # 32 bytes hex-encoded


class SecretsError(RuntimeError):
    pass


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        return AESGCM
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise SecretsError(
            "python package 'cryptography' is required for secrets encryption "
            "(pip install cryptography)"
        ) from exc


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    try:
        return base64.b64decode(s.encode("ascii"), validate=True)
    except Exception as exc:
        raise SecretsError(f"malformed base64 in secrets envelope: {exc}") from exc


def normalize_master_key(raw: str, *, key_type: str = "hex") -> bytes:
    """Parse the externally-injected master key. Never logs the value."""
    raw = (raw or "").strip()
    if not raw:
        raise SecretsError("master key is empty")
    if key_type == "passphrase":
        raise SecretsError("use derive_key_from_passphrase() for passphrases")
    if key_type != "hex":
        raise SecretsError(f"unknown key type: {key_type}")
    if len(raw) != KEY_HEX_LEN:
        raise SecretsError(f"master key must be {KEY_HEX_LEN} hex chars (32 bytes)")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise SecretsError("master key is not valid hex") from exc


def generate_master_key_hex() -> str:
    """Generate a fresh random master key (hex). The CALLER stores it outside storage."""
    return os.urandom(32).hex()


def derive_key_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SecretsError("cryptography required for scrypt") from exc
    if not passphrase:
        raise SecretsError("empty passphrase")
    kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_secrets(secrets: Dict[str, str], key: bytes, *,
                    passphrase_mode: bool = False) -> bytes:
    """Encrypt {name: value} into a versioned AES-256-GCM envelope (bytes)."""
    if not isinstance(secrets, dict) or not secrets:
        raise SecretsError("nothing to encrypt")
    for k, v in secrets.items():
        if not isinstance(k, str) or not k or not isinstance(v, str) or not v:
            raise SecretsError("secret entries must be non-empty name/value strings")
    AESGCM = _aesgcm()
    nonce = os.urandom(12)
    if passphrase_mode:
        # caller passed the passphrase; derive + embed salt (NOT the key itself)
        salt = os.urandom(16)
        use_key = derive_key_from_passphrase(key.decode("latin1") if isinstance(key, bytes) else str(key), salt)
        kdf, salt_field = "scrypt", _b64e(salt)
    else:
        if not isinstance(key, bytes) or len(key) != 32:
            raise SecretsError("key must be 32 raw bytes (use normalize_master_key)")
        use_key, kdf, salt_field = key, "none", None
    ct = AESGCM(use_key).encrypt(nonce, json.dumps(secrets, sort_keys=True).encode("utf-8"), MAGIC_AAD)
    env = {"v": FORMAT_VERSION, "alg": "AES-256-GCM", "kdf": kdf,
           "salt": salt_field, "nonce": _b64e(nonce), "ct": _b64e(ct)}
    return (json.dumps(env, sort_keys=True) + "\n").encode("utf-8")


def decrypt_secrets(blob: bytes, key: bytes | str, *,
                    passphrase: Optional[str] = None) -> Dict[str, str]:
    """Decrypt + authenticate an envelope. Any integrity/key failure → SecretsError (fail-closed).
    Never logs plaintext."""
    AESGCM = _aesgcm()
    try:
        env = json.loads(blob.decode("utf-8"))
        if not isinstance(env, dict) or env.get("v") != FORMAT_VERSION or env.get("alg") != "AES-256-GCM":
            raise SecretsError("unsupported secrets envelope version/alg")
        nonce, ct = _b64d(env["nonce"]), _b64d(env["ct"])
        if env.get("kdf") == "scrypt":
            if not passphrase:
                raise SecretsError("envelope requires a passphrase")
            use_key = derive_key_from_passphrase(passphrase, _b64d(env["salt"]))
        else:
            if isinstance(key, str):
                key = normalize_master_key(key)
            if not isinstance(key, bytes) or len(key) != 32:
                raise SecretsError("key must be 32 raw bytes")
            use_key = key
        pt = AESGCM(use_key).decrypt(nonce, ct, MAGIC_AAD)
    except SecretsError:
        raise
    except Exception as exc:  # authentication failure, malformed blob, ...
        raise SecretsError("secrets decryption/authentication failed") from exc
    try:
        out = json.loads(pt.decode("utf-8"))
    except Exception as exc:
        raise SecretsError("decrypted payload is not valid JSON") from exc
    if not isinstance(out, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in out.items()):
        raise SecretsError("decrypted payload shape invalid")
    return out


def secrets_names_in_envelope(blob: bytes) -> Dict[str, object]:
    """Metadata-only view (no decrypt): version/alg/kdf sizes. Fail-closed on garbage."""
    try:
        env = json.loads(blob.decode("utf-8"))
        return {"v": env.get("v"), "alg": env.get("alg"), "kdf": env.get("kdf"),
                "ct_bytes": len(_b64d(env.get("ct", "")))}
    except Exception as exc:
        raise SecretsError("not a valid secrets envelope") from exc
