"""AES-256-GCM secrets envelope + leak-proofing tests (values are FAKE fixtures)."""
import json

import pytest

from hermes_persist import secrets as sec

FAKE = {
    "TELEGRAM_BOT_TOKEN": "123456789:FAKEONLYFAKEONLYFAKEONLYFAKEONLY1",
    "GEMINI_API_KEY": "AQ.FAKEONLYFAKEONLYFAKEONLYFAKEONLY.F",
    "B2_APPLICATION_KEY": "KFAKEFAKEFAKEFAKEFAKE00000001",
}
KEY = bytes(range(32))


def test_roundtrip():
    blob = sec.encrypt_secrets(FAKE, KEY)
    assert sec.decrypt_secrets(blob, KEY) == FAKE


def test_envelope_structure_and_versioning():
    env = json.loads(sec.encrypt_secrets(FAKE, KEY).decode())
    assert env["v"] == 1 and env["alg"] == "AES-256-GCM" and env["kdf"] == "none"
    assert set(env) == {"v", "alg", "kdf", "salt", "nonce", "ct"}


def test_unique_nonce_each_encryption():
    b1 = json.loads(sec.encrypt_secrets(FAKE, KEY))
    b2 = json.loads(sec.encrypt_secrets(FAKE, KEY))
    assert b1["nonce"] != b2["nonce"]
    assert b1["ct"] != b2["ct"]


def test_no_plaintext_leaks_into_ciphertext():
    blob = sec.encrypt_secrets(FAKE, KEY)
    for v in FAKE.values():
        assert v.encode() not in blob
    # and not base64-encoded inside the JSON envelope either
    env_text = blob.decode()
    import base64
    for v in FAKE.values():
        assert base64.b64encode(v.encode()).decode() not in env_text


def test_wrong_key_fails_closed():
    blob = sec.encrypt_secrets(FAKE, KEY)
    other = bytes([255 - b for b in KEY])
    with pytest.raises(sec.SecretsError):
        sec.decrypt_secrets(blob, other)


def test_tampered_ciphertext_fails_closed():
    env = json.loads(sec.encrypt_secrets(FAKE, KEY).decode())
    import base64 as b64
    ct = bytearray(b64.b64decode(env["ct"]))
    ct[len(ct) // 2] ^= 0x01
    env["ct"] = b64.b64encode(bytes(ct)).decode()
    with pytest.raises(sec.SecretsError):
        sec.decrypt_secrets((json.dumps(env)).encode(), KEY)


def test_tampered_aad_version_fails_closed():
    env = json.loads(sec.encrypt_secrets(FAKE, KEY).decode())
    env["v"] = 2  # version confusion attempt
    with pytest.raises(sec.SecretsError):
        sec.decrypt_secrets(json.dumps(env).encode(), KEY)


def test_garbage_blob_fails_closed():
    with pytest.raises(sec.SecretsError):
        sec.decrypt_secrets(b"not-json", KEY)


def test_master_key_validation():
    good = "ab" * 32
    assert len(sec.normalize_master_key(good)) == 32
    with pytest.raises(sec.SecretsError):
        sec.normalize_master_key("short")
    with pytest.raises(sec.SecretsError):
        sec.normalize_master_key("zz" * 32)
    gen = sec.generate_master_key_hex()
    assert len(gen) == 64 and len(bytes.fromhex(gen)) == 32


def test_scrypt_passphrase_mode():
    blob = sec.encrypt_secrets(FAKE, "correct horse battery staple".encode(), passphrase_mode=True)
    env = json.loads(blob.decode())
    assert env["kdf"] == "scrypt" and env["salt"]
    out = sec.decrypt_secrets(blob, b"", passphrase="correct horse battery staple")
    assert out == FAKE
    with pytest.raises(sec.SecretsError):
        sec.decrypt_secrets(blob, b"", passphrase="wrong passphrase")


def test_read_and_write_creds_env_0600(tmp_path, monkeypatch):
    from hermes_persist import sync
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    envf = tmp_path / ".env"
    envf.write_text("TELEGRAM_BOT_TOKEN=x:FAKE\nOTHER=leave\nGEMINI_API_KEY=AQ.F\n")
    vals = sync.read_secret_values(tmp_path, ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"))
    assert vals == {"TELEGRAM_BOT_TOKEN": "x:FAKE", "GEMINI_API_KEY": "AQ.F"}
    path = sync.write_creds_env(tmp_path, vals)
    import os
    assert (os.stat(path).st_mode & 0o777) == 0o600
    back = sync.read_secret_values(tmp_path, ("TELEGRAM_BOT_TOKEN",))
    assert back["TELEGRAM_BOT_TOKEN"] == "x:FAKE"
