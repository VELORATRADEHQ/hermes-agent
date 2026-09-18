# hermes-ops — persistent-state operations (Backblaze B2)

Tooling for state backup/restore/verify, encrypted secret backup, gateway
runtime supervision, and Telegram health verification. Ops tooling only —
nothing here runs on the gateway hot path.

## Quick map

| file | purpose |
|---|---|
| `backup.sh` | curated state snapshot → B2 (SHA-256 manifests, schema/versioned) |
| `restore.sh` | restore snapshot (manifest+hash verified) |
| `verify.sh` | integrity check of a remote snapshot |
| `healthcheck.sh` | composite machine/gateway/backup/telegram health gate |
| `owner-onboard.sh` | owner onboarding flow |
| `install-service.sh` | install the systemd **user** service + linger |
| `hermes-gateway.service` | Type=notify unit, `WatchdogSec=90s` |
| `gateway-supervisor.sh` | non-systemd fallback: `python3 -m hermes_persist supervisor` |

## Backends (B2 primary, S3/R2 aliases kept)

- **Primary: Backblaze B2** over its S3-compatible API. Endpoint is
  **region-specific**: `s3.<region>.backblazeb2.com` (shown on your bucket page).
  Addressing auto-switches to virtual-hosted style (`bucket.s3.<region>.backblazeb2.com/key`);
  SigV4 region auto-derives from the endpoint host (`us-west-004`, …). No hard-coded
  credentials anywhere — everything comes from env.
- Aliases: `B2_*` → `S3_*` → `R2_*` (first non-empty wins), so old deployments keep working.
  Force addressing via `HERMES_S3_ADDRESSING=virtual|path` (minio/custom gateways).

## Credential separation (two layers, no circular dependency)

| | Layer A — runtime RW | Layer B — bootstrap RO |
|---|---|---|
| B2 object | Application Key, bucket=`hermes-state`, caps `readFiles`,`writeFiles` | Application Key, bucket=`hermes-state`, cap `readFiles`, **namePrefix `secrets/`** |
| can list/write/delete bucket objects | yes (bucket only) | **no** (read-only, restricted to the secrets prefix) |
| stored | runtime env / `~/.hermes/creds.env` (0600) | runtime env only |
| env names | `B2_APPLICATION_KEY_ID` / `B2_APPLICATION_KEY` | `B2_BOOTSTRAP_APPLICATION_KEY_ID` / `B2_BOOTSTRAP_APPLICATION_KEY` |

**B2's narrowest expressible scope** (honest statement): an Application Key is
scoped to one bucket, a capability list, and an optional `namePrefix`. Object-level
ACLs beyond the prefix restriction do **not** exist on B2; the table above is the
actual narrowest set. The bootstrap key additionally cannot reach anything outside
`secrets/`.

**Master encryption key**: `SECRETS_MASTER_KEY` (64 hex chars). Lives **outside B2**
(password manager / HSM / typed at provision time). Runtime env only. Never in the
bucket, never in git, never printed. `secrets.enc` is undecryptable without it even
to someone holding both B2 keys.

## Encrypted secret backup — `secrets/secrets.enc`

Contains `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `B2_APPLICATION_KEY_ID`,
`B2_APPLICATION_KEY`, + future creds. Envelope: AES-256-GCM, 12-byte random nonce
per write, versioned/authenticated JSON `{"v":1,"alg":"AES-256-GCM",...}`, AAD
`hermes-persist/secrets.enc/v1`, fail-closed on any tamper. Uses the `cryptography`
package (lazy import; only ops code paths require it).

Flows (names only are ever printed):

```bash
# provision: encrypt from env/.env/creds.env and upload
python3 -m hermes_persist secrets-put
# verify remote blob exists and authenticates (no disk writes)
python3 -m hermes_persist secrets-check
# cold-start: decrypt in memory; --write also writes ~/.hermes/creds.env 0600 atomic
python3 -m hermes_persist secrets-fetch [--write]
```

Layer-B env falls back to Layer-A when `B2_BOOTSTRAP_*` unset (single-key setups).

## Gateway runtime: systemd service and watchdog contract

`gateway/systemd_notify.py` (in-repo) emits READY/WATCHDOG sd_notify datagrams
paced by `WATCHDOG_USEC`; `gateway/shutdown_watchdog.py` maintains a
`state/gateway.heartbeat` file **off the event loop** (frozen loop ⇒ heartbeat
stops ⇒ externally detectable) and can hard-exit a frozen loop. No gateway code
changes were needed:

- `hermes-gateway.service`: `Type=notify`, `WatchdogSec=90s`,
  `EnvironmentFile=%h/.hermes/creds.env`, `Restart=on-failure`,
  `StartLimitBurst=5`/`StartLimitIntervalSec=600` (restart loop is capped —
  systemd stops hammering after sustained failure), no ExecStopPost kill hacks.
- `install-service.sh` installs as a **user** unit and enables **linger** so it
  starts at boot without a login session.
- **Non-systemd fallback** (`gateway-supervisor.sh` →
  `python3 -m hermes_persist supervisor -- <gateway cmd>`): capped restarts
  (5 within 600 s, exponential backoff 2 s→60 s) plus a health gate that kills
  and restarts a *stuck-but-alive* child after consecutive unhealthy probes.

### Health ≠ process-exists, and "connected once" ≠ healthy

`python3 -m hermes_persist telegram-health` classifies **A–H** from evidence:
pidfile liveness, heartbeat file age (≤120 s), the last
`Connected to Telegram (polling mode)` log marker vs. later error markers, and an
authenticated `getMe` probe through the configured relay base_url (401/403/404 ⇒
token invalid ⇒ **F**; network/API failure ⇒ **E**; `gateway_state.json` saying
"connected" is **not** accepted as proof of polling). The token is read but never
echoed into output.

### Platform limitation (stated, not papered over)

systemd/supervisor cannot survive the **machine provider suspending the container**
(e.g. idle-timeout destruction of the whole VM — that was the measured root cause
of the last outage, classification A←G). Persistent operation requires a host that
stays up (VPS/bare metal). On Deepnote-style hosts the correct interim mitigation
is a scheduled muster (keep-alive notebook) + the supervisor script.

## Backup model

Preserves the curated set: `state.db(+wal/shm)`, sessions, curated config, pairing,
memories, channel dir, `SOUL.md`, `kanban.db`, cron, cpanel state, and the encrypted
secrets object — plus SHA-256 manifests with schema/version + snapshot timestamp.
Excludes logs/dumps/caches/venv/locks/pids/sockets/tmp and **plaintext creds**.
`restore.sh` verifies manifest + object hashes before touching local state;
corruption fails the restore before any mutation.

## Secret hygiene

Secrets live in memory where practical; any on-disk materialization is
`~/.hermes/creds.env` with `0600` (atomic write, `umask 0177`). Nothing secret is
written to logs/manifests/backup objects/diagnostics/git. Automated leak tests in
`tests/hermes_persist/` (ciphertext must not contain plaintext, supervisor output
must not echo tokens, health output must not echo tokens, backup catalog must not
include cred files) fail the build on any regression.
