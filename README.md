# bepasshortink — inshorturl.in shortlink resolver

Resolve `inshorturl.in/<alias>` to its real destination. Two lanes:

| Lane | Time | Needs | Use when |
|---|---|---|---|
| `inshorturl_http.py` | **~20–35 s** | cfast binary (:8810 API) | default |
| `inshorturl_bypass.py` | ~50–90 s | Camoufox browser | cfast broken / debugging |

Both clear the same two Cloudflare gates, just with different machinery.

```
inshorturl.in/<alias>
  └─ CF managed challenge            → cf_clearance
       └─ inshorturl_http.py: cfast binary (Rust VM, no browser)
       └─ inshorturl_bypass.py: Camoufox page load
  └─ mahitimanch.in/folder.php       → 4 self-POST step forms (filler pages)
  └─ inshorturl.in/<alias> + Referer → verify page (Turnstile)
  └─ POST /links/go                  → {"status":"success","url":"<destination>"}
```

## Quick start

```bash
git clone https://github.com/HitamLegit6777/bepasshortink
cd bepasshortink
bash setup.sh --check        # installs cfast + API + deps, resolves a test link

python3 inshorturl_http.py https://inshorturl.in/Tddfjl8
```

`setup.sh` fetches from [`HitamLegit6777/cfast`](https://github.com/HitamLegit6777/cfast/releases/tag/v1.1.0-managed):

- `challenge` — prebuilt linux-x64 pure-protocol solver (~24 MB)
- `profile_safari.json` + `profile_safari/`, `profile_fast.json` + `profile_fast/`
- `solver_api.py`, started as `cfast.service` on `:8810` (or `nohup` with `NO_SYSTEMD=1`)

Nothing else is vendored here; the binary lives in the cfast repo/release.

```bash
bash setup.sh --with-browser   # also install Camoufox for the browser fallback
bash setup.sh --uninstall
PREFIX=/opt/cfast PORT=8810 NO_SYSTEMD=1 bash setup.sh   # container-friendly
```

## Usage

```bash
python3 inshorturl_http.py https://inshorturl.in/Tddfjl8
python3 inshorturl_http.py Tddfjl8 --json
python3 inshorturl_http.py Tddfjl8 --verbose      # log every hop

python3 inshorturl_bypass.py Tddfjl8              # Camoufox fallback
python3 inshorturl_bypass.py Tddfjl8 --headed     # watch it run
python3 inshorturl_bypass.py Tddfjl8 --proxy socks5://127.0.0.1:1600
```

Output: destination URL on stdout, `--json` gives `{"short":…,"destination":…}`.
Exit code 0 on success, 1 with `FAIL <Error>: <why>` on stderr.

## Why the pure-HTTP lane works

The managed challenge is not a Turnstile widget; the orchestrator script builds a
JS VM, asks it to compute an answer, and hands back `cf_clearance`. cfast replays
that VM in Rust. Two facts matter operationally:

- **The cookie is bound to the profile UA + the solver's egress IP.** Replaying
  it from another IP, or with another UA/TLS fingerprint, gets a fresh challenge.
  That is why one iOS-Safari profile is used for every hop in this script.
- **The filler steps are plain self-POSTs.** The 10 s "Human Verify" timer is
  client-side; progress lives server-side, so the final page only exists as the
  POST response. ~0.7 s per step.
- **The verify page needs `Referer:` = final filler page.** Without it the server
  307s back to the check page even with a valid `cf_clearance`.

## Config

`inshorturl_http.py` reads:

| Env | Default |
|---|---|
| `CFast_BIN` | `/opt/cfast/challenge` |
| `CFast_PROFILE` | `/opt/cfast/profile_safari.json` |
| `CFast_API` | `http://localhost:8810` |
| `Inshort_UA` | iOS Safari 26.6 UA (must match the profile) |

`solver_api.py` reads `TS_BIN`, `TS_PROFILE`, `TS_PORT`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `expected one program runner, found 0` | CF rotates the orchestrator script; retried 3× automatically. Persistent? rebuild cfast from source. |
| `probe N matches no known collector` | Same rotation, also retried. |
| `verify page not served` | `Referer` did not survive; re-run. If it persists, the filler step order changed. |
| `solver API unreachable` | `systemctl status cfast` / `journalctl -u cfast -n 50`. |
| `cf_clearance` not printed | Check the profile dir next to the JSON (`profile_safari/answers.json`); cfast resolves `answers` relative to the profile file. |
| Different UA than the profile | `cf_clearance` stops matching; keep `Inshort_UA` == profile UA. |

## Files

| File | What |
|---|---|
| `inshorturl_http.py` | pure-HTTP resolver (cfast binary + API) |
| `inshorturl_bypass.py` | Camoufox browser resolver (fallback) |
| `setup.sh` | installer / service manager for the cfast dependency |

## Credit

`challenge` and `solver_api.py` come from [cfsolve](https://github.com/chris124567/cfsolve)
(rebuilt with managed-challenge support in `HitamLegit6777/cfast`). This repo is
the resolver layer.

MIT.
