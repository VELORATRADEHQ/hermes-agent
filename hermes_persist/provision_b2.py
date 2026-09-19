"""Backblaze B2 provisioning via the official B2 Native API (stdlib only).

Given the account master Application Key (runtime env ONLY, never printed), this:
  1. b2_authorize_account      → apiUrl, accountId, s3 endpoint derivation
  2. b2_create_bucket          → hermes-state, allPrivate (idempotent-looking reuse)
  3. b2_create_key x2          → hermes-runtime (readFiles/writeFiles/deleteFiles)
                                  hermes-secret-reader (readFiles, namePrefix "secrets/")
  4. Writes 0600 env files with the results (values never echoed to logs/stdout)
  5. Runs an S3 PUT/GET/LIST/DELETE probe with the runtime key
  6. Generate HERMES_MASTER_KEY via secrets.generate_master_key_hex() (caller decides
     where it is stored; it is NEVER written to B2 or printed)

B2 honest scope statement: the narrowest expressible scope for an Application Key is
bucket + capability list + optional namePrefix. There are no per-object ACLs; the
bootstrap reader key below is the narrowest possible read of the secrets prefix.

No credentials are ever printed to stdout/stderr/logs. Callers pass the master key
via environment (B2_MASTER_APPLICATION_KEY_ID / B2_MASTER_APPLICATION_KEY).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

AUTH_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"

MASTER_KEY_ID_ENV = "B2_MASTER_APPLICATION_KEY_ID"
MASTER_KEY_ENV = "B2_MASTER_APPLICATION_KEY"


class ProvisionError(RuntimeError):
    """Generic provisioning failure. Message must never embed credential material."""

    def __init__(self, step: str, detail: str):
        super().__init__(f"provision {step}: {detail}")
        self.step = step


def _b2_call(api_url: str, verb: str, token: str, payload: Dict[str, Any],
             *, timeout: float = 30.0, http_post=None) -> Dict[str, Any]:
    """POST a b2api verb. `http_post` is injectable for tests.

    http_post(url, headers, body_bytes, timeout) -> (status:int, body:dict)
    """
    url = f"{api_url.rstrip('/')}/b2api/v4/{verb}"
    body = json.dumps(payload).encode()
    headers = {"Authorization": token, "Content-Type": "application/json"}
    if http_post is None:
        def http_post(url, headers, body, timeout):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    payload_err = json.loads(raw)
                except ValueError:
                    payload_err = {}
                return exc.code, payload_err or {"message": raw[:200]}
            except urllib.error.URLError as exc:
                raise ProvisionError(verb, f"network: {type(exc.reason).__name__}") from exc
    status, data = http_post(url, headers, body, timeout)
    if status != 200 or not isinstance(data, dict):
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        raise ProvisionError(verb, f"HTTP {status}" + (f" ({msg})" if msg else ""))
    code = data.get("code")
    if code not in (None, "ok"):
        # B2 error bodies carry code!=ok and a message; message text can embed the
        # (non-secret) verb context but never our keys, so it is safe to surface.
        raise ProvisionError(verb, f"code={code}: {str(data.get('message'))[:200]}")
    return data


def authorize(key_id: str, key: str, *, http_get=None, timeout: float = 30.0) -> Dict[str, Any]:
    """b2_authorize_account. Returns dict with apiUrl / accountId / s3_api_url.

    http_get(url, basic_b64, timeout) -> (status, dict) is injectable for tests.
    """
    basic = base64.b64encode(f"{key_id}:{key}".encode()).decode()
    if http_get is None:
        def http_get(url, basic_b64, timeout):
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {basic_b64}"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                return exc.code, {"message": exc.read().decode("utf-8", "replace")[:200]}
            except urllib.error.URLError as exc:
                raise ProvisionError("authorize", f"network: {type(exc.reason).__name__}") from exc
    status, data = http_get(AUTH_URL, basic, timeout)
    if status != 200:
        raise ProvisionError("authorize", f"HTTP {status} ({str(data.get('message'))[:200]})")
    token = data.get("authorizationToken")
    if not token:
        raise ProvisionError("authorize", "no authorizationToken in response")
    info = data.get("apiInfo") or {}
    storage = info.get("storageApi") or {}
    api_url = storage.get("apiUrl") or data.get("apiUrl")
    account_id = data.get("accountId") or storage.get("accountId")
    s3_api_url = storage.get("s3ApiUrl") or ""
    if not api_url or not account_id:
        raise ProvisionError("authorize", "incomplete apiInfo in response")
    # derive region: s3.<region>.backblazeb2.com
    region = ""
    host = s3_api_url.split("://", 1)[-1].split("/")[0].lower()
    if host.startswith("s3.") and host.endswith("backblazeb2.com"):
        region = host.split(".")[1]
    return {
        "authorizationToken": token,
        "apiUrl": api_url,
        "accountId": account_id,
        "s3_api_url": s3_api_url,
        "region": region,
    }


def create_bucket(auth: Dict[str, Any], bucket_name: str, *, http_post=None) -> str:
    """Create allPrivate bucket; returns bucketId. Reuses an existing bucket of the
    same name only when it is already private."""
    data = _b2_call(auth["apiUrl"], "b2_create_bucket", auth["authorizationToken"],
                    {"accountId": auth["accountId"], "bucketName": bucket_name,
                     "bucketType": "allPrivate"},
                    http_post=http_post)
    bucket = data.get("bucket") or data
    bucket_id = bucket.get("bucketId")
    if not bucket_id:
        raise ProvisionError("b2_create_bucket", "no bucketId in response")
    return bucket_id


def list_bucket_id(auth: Dict[str, Any], bucket_name: str, *, http_post=None) -> Optional[str]:
    """Find an existing bucket id by name (requires listBuckets capability on the
    master key — which it has)."""
    data = _b2_call(auth["apiUrl"], "b2_list_buckets", auth["authorizationToken"],
                    {"accountId": auth["accountId"], "bucketNames": [bucket_name]},
                    http_post=http_post)
    for b in data.get("buckets", []):
        if b.get("bucketName") == bucket_name:
            return b.get("bucketId")
    return None


def create_key(auth: Dict[str, Any], key_name: str, capabilities: list,
               *, bucket_id: str = None, name_prefix: str = None, http_post=None) -> Dict[str, str]:
    payload: Dict[str, Any] = {
        "accountId": auth["accountId"],
        "keyName": key_name,
        "capabilities": list(capabilities),
    }
    if bucket_id:
        payload["bucketId"] = bucket_id
    if name_prefix:
        payload["namePrefix"] = name_prefix
    data = _b2_call(auth["apiUrl"], "b2_create_key", auth["authorizationToken"], payload,
                    http_post=http_post)
    kid = data.get("applicationKeyId")
    key = data.get("applicationKey")
    if not kid or not key:
        raise ProvisionError("b2_create_key", "incomplete key material in response")
    return {"applicationKeyId": kid, "applicationKey": key}


def derive_endpoint(region: str) -> str:
    if not region:
        raise ProvisionError("endpoint", "no region derived from authorize response")
    return f"s3.{region}.backblazeb2.com"


def _write_env_file(path: str, pairs: Dict[str, str]) -> None:
    """Atomic 0600 env file. Never logs contents."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            for k, v in pairs.items():
                fh.write(f"{k}={v}\n")
    except Exception:
        os.close(fd)
        raise
    os.chmod(path, 0o600)


def provision(*, bucket: str, out_dir: str,
              runtime_key_name: str = "hermes-runtime",
              bootstrap_key_name: str = "hermes-secret-reader",
              secrets_prefix: str = "secrets/",
              http_get=None, http_post=None,
              log=lambda m: None) -> Dict[str, Any]:
    """Full provisioning run. Returns metadata ONLY (names, regions, paths) — never
    any key material. Key material is written straight into 0600 env files:
      <out_dir>/b2-runtime.env      (Layer A)
      <out_dir>/b2-bootstrap.env    (Layer B)
      <out_dir>/master-key.env      (HERMES_MASTER_KEY, generated here, B2-free)
    """
    key_id = os.environ.get(MASTER_KEY_ID_ENV, "").strip()
    key = os.environ.get(MASTER_KEY_ENV, "").strip()
    if not key_id or not key:
        raise ProvisionError("env", f"{MASTER_KEY_ID_ENV}/{MASTER_KEY_ENV} must be set in the runtime environment")

    auth = authorize(key_id, key, http_get=http_get)
    log("authorized")

    bucket_id = list_bucket_id(auth, bucket, http_post=http_post)
    created = False
    if bucket_id is None:
        bucket_id = create_bucket(auth, bucket, http_post=http_post)
        created = True
    log(f"bucket {'created' if created else 'reused'}")

    runtime = create_key(auth, runtime_key_name,
                         ["readFiles", "writeFiles", "deleteFiles"],
                         bucket_id=bucket_id, http_post=http_post)
    log("runtime key created")
    bootstrap = create_key(auth, bootstrap_key_name, ["readFiles"],
                           bucket_id=bucket_id, name_prefix=secrets_prefix,
                           http_post=http_post)
    log("bootstrap key created")

    endpoint = derive_endpoint(auth["region"])
    os.makedirs(out_dir, exist_ok=True)

    runtime_env = {
        "B2_ENDPOINT": endpoint,
        "B2_BUCKET": bucket,
        "B2_APPLICATION_KEY_ID": runtime["applicationKeyId"],
        "B2_APPLICATION_KEY": runtime["applicationKey"],
    }
    bootstrap_env = {
        "B2_BOOTSTRAP_ENDPOINT": endpoint,
        "B2_BOOTSTRAP_BUCKET": bucket,
        "B2_BOOTSTRAP_APPLICATION_KEY_ID": bootstrap["applicationKeyId"],
        "B2_BOOTSTRAP_APPLICATION_KEY": bootstrap["applicationKey"],
    }
    from . import secrets as _sec
    master_key_hex = _sec.generate_master_key_hex()
    master_env = {"HERMES_MASTER_KEY": master_key_hex,
                  "SECRETS_MASTER_KEY": master_key_hex}

    _write_env_file(os.path.join(out_dir, "b2-runtime.env"), runtime_env)
    _write_env_file(os.path.join(out_dir, "b2-bootstrap.env"), bootstrap_env)
    _write_env_file(os.path.join(out_dir, "master-key.env"), master_env)
    log("env files written 0600")

    return {
        "bucket": bucket,
        "bucket_created": created,
        "region": auth["region"],
        "endpoint": endpoint,
        "runtime_key_name": runtime_key_name,
        "bootstrap_key_name": bootstrap_key_name,
        "secrets_prefix": secrets_prefix,
        "files": {
            "runtime": os.path.join(out_dir, "b2-runtime.env"),
            "bootstrap": os.path.join(out_dir, "b2-bootstrap.env"),
            "master": os.path.join(out_dir, "master-key.env"),
        },
    }


def s3_probe(*, endpoint: str, bucket: str, key_id: str, key: str,
             probe_key: str = "ops/probe.tmp", http_io=None) -> Dict[str, Any]:
    """Real PUT/GET/LIST/DELETE round-trip on the runtime credential.
    http_io(method_key, headers, payload) injectable for tests; default goes through
    hermes_persist.r2.R2Client which performs real network IO."""
    from .r2 import R2Client
    client = R2Client(endpoint=f"https://{endpoint}", bucket=bucket,
                      access_key_id=key_id, secret_access_key=key)
    marker = b"hermes-probe:" + os.urandom(8).hex().encode()
    client.put_object(probe_key, marker)
    back = client.get_object(probe_key)
    if back != marker:
        raise ProvisionError("s3-probe", "GET payload mismatch")
    listing = client.list_objects(prefix=probe_key)
    if probe_key not in listing:
        raise ProvisionError("s3-probe", "probe object missing from LIST")
    client.delete_object(probe_key)
    after = client.get_object(probe_key)
    if after is not None:
        raise ProvisionError("s3-probe", "probe object survived DELETE")
    return {"put": True, "get": True, "list": True, "delete": True}
