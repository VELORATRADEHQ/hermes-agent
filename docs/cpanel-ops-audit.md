# cpanel operations audit (task 8, branch hermes-cpanel, base b266b3a0 → ee84f62)

Classification vocabulary: **REAL+runtime** (changes the effective provider/model/gate used by the
next real inference or task request) — **REAL-config** (persists durable configuration, no by-itself
runtime redirect) — **REAL-access** (changes who may use the gateway) — **REAL-probe** (performs a
live read-only transaction against the declared endpoint; no state) — **informational** (reads UI or
renders state) — **display-only** (UI preference). **UI-only ops are forbidden**: every button must do
one of the above truthfully or not render (sliver gating at render time).

## A. Model routing state — one source of truth (§4, §11)

Precedence, highest wins per turn (documented and enforced at `_resolve_session_agent_runtime`):

1. explicit per-session override (store row + hydrated `SessionState.conversation.model_override`)
2. panel/global selection `model.default` (+ `model.provider`), persisted through
   `cpanel._write_config_key` — the **single config-mutation path the panel uses for every key**
   (adapter `_save_gateway_config_key`; fallback `read_user_config_raw` + `atomic_config_write`)
3. environment default / hard fallback

Cleared-state handling: after every `models:set`, the panel clears **all four layers** of this
chat's pin (`_clear_session_pin_and_evict`: store row, hydrated SessionState override, legacy
runner dict, cached agent eviction). Panel screens re-render from post-write state, never from
the value the button claimed. `/model` CLI writes its own session override through
`SessionStore.set_model_override`; both surfaces converge on the same precedence above, which the
per-turn resolver reads — the UI cannot assert a model the resolver would not use.

- `cust:<pid> runtime.type=="task_run"` entries never enter the model precedence for chat
  (task-only — excluded from `/model` and panel defaults; refuse native set-default).

## B. Operation inventory (§8, §9)

| op | class | evidence |
|---|---|---|
| `prov:sel:<name>` | informational (detail screen) | detail re-renders from live config each tap |
| `prov:toggle:<name>` | **REAL+runtime** (after fix) | writes native `model.enabled_providers` / `model.disabled_providers` — the exact sets `hermes_cli.runtime_provider._raise_if_provider_disabled` gates on; legacy legacy-list migration kept |
| `prov:mkdefault:<name>` | **REAL+runtime** | writes `model.provider` + force-enables it natively; refusal path n/a (native chat providers only in this screen) |
| `prov:delask/del:<name>` | REAL-access (removes from panel) | post-delete: provider deleted from native `providers:` registry/defaults; runtime re-resolves per turn |
| `add:start/pick/keyonly/model/save/cancel` | REAL-config | wizard persists provider profile/env mapping; model picker after save routes through the same primitive |
| `models:set:<m>` | **REAL+runtime** (after fix) | persists `model.default` via `_write_config_key` (the canonical panel config-writer), then clears this chat's pin across 4 layers; screen re-renders from post-write state; save failure shows an explicit save-fail message and changes nothing |
| `models:clearpin` | **REAL+runtime** | 4-layer clear (see A); failure is reported as a visible ⚠ line, never swallowed |
| `models:type` | REAL-config | manual id → same primitive path |
| `test:sel/go:<name>` | REAL-probe | generic protocol probe (descriptor auth/capabilities/discovery); transaction recorded; never mutates state |
| `cust:new/sel/listf` | informational | entry creation/listing of custom providers |
| `cust:go:<pid>` | REAL-probe | same generic probe; reports async descriptor reasons verbatim |
| `cust:models:<pid>` | REAL-probe (gated) | only rendered when `discovery_supported` and not task-only; fetch is read-only |
| `cust:setmodel:<pid>:<m>` | REAL-config | entry `default_model`; effective when that provider is the active route |
| `cust:setdef:<pid>` | **REAL+runtime** | writes `model.provider=custom:<pid>` + `model.default=dm`; task-only providers are refused (button sliver never rendered for them) |
| `cust:toggle:<pid>` | **REAL+runtime** (after fix) | native `model.enabled/disabled_providers` + `providers.<pid>.enabled` mirror for panel rows |
| `cust:runtask:<pid>` | **REAL+runtime** (NEW) | task-only entries only; capability-gated (runtime.type=task_run or task capabilities); no-credential fails closed (no request attempted); arms pending instruction; executes via `gateway.task_runtime` with bounded polling; truth-first result rendering (status, HTTP code, run id, polls/elapsed, output; never the key) |
| `cust:editask:*`, `cwx:*` | REAL-config | metadata/auth/discovery/pointer edits; runtime uses new values at next resolution |
| `cust:delask/delgo:<pid>` | REAL-config | removes the entry from config; appears nowhere at next render/resolution |
| `status` | informational | live state read |
| `users:ok/rv/rvgo:<uid>` | REAL-access | approve pairing request / revoke authorized user — deterministic auth state change, no group broadening |
| `settings:lang` | display-only (UI locale) | explicit: en/fa panel language |
| `settings:comp` | display-only | compact list toggle |
| `logs` | informational | bounded log excerpt, token patterns redacted |
| `backup:go/ask/do` | REAL-config | tar export of config directory; no secrets beyond what config already contains |

Rule retained from task 7: unavailable actions are never rendered (sliver gating): `setdef` suppressed
for task-only; `models` suppressed when discovery unsupported or task-only; Run-task rendered only
for task-only entries; probe is generic (no `/models`-with-Bearer assumption anywhere).

## C. Error taxonomy (§12) — explicit, never silent

`gateway/task_runtime.py` statuses: `success / failed / cancelled / timeout / auth / quota /
validation / provider_error / network / malformed / no_key / not_capable / rejected`.
- contract-based: bare HTTP 200 without a parseable run id or explicit terminal status → `malformed`,
  never success
- all polling bounded (interval/timeout/max_polls clamped; wall-clock budget enforced mid-loop)
- loaded-swallow policy: provider error text surfaced (capped), no fake cheerful acknowledgment
- model switching: invalid/expired/inaccessible model → the primitive's refusal is rendered
  verbatim; previous selection stays; no silent fallback to another model

Provider probing (task 7, unchanged): probe strictly read-only; explicit per-protocol sequences;
SSRF guard layers (https-only + pinned host + auth-header sanitization + redirect stripping);
descriptor-authored auth — X-Browser-Use-API-Key entries NEVER send Authorization: Bearer.

## D. Security regression (§13, re-audit over the delta)

- secrets: resolved at call time from env/confit; never logged/echoed/returned (test:
  `test_result_never_contains_secret`, `test_x_browser_use_key_header_and_never_bearer`); entry
  `api_key` values never written to state/config by the runtime; gateway state files never created
  by these paths
- access: Run-task is reachable only inside the admin-only control panel flow (existing
  `_start_user_kind`/`_admin_only` gating; task flow additionally fails closed without capability
  metadata). No GATEWAY_ALLOW_ALL_USERS, no group authorization change — conversational routing
  untouched, no new visible command added (`/help` + `/model` surface unchanged)
- deploy/install: no dependency changes; `gateway/task_runtime.py` imports httpx + existing probe
  guard only; no migration; no config defaults altered without explicit write
