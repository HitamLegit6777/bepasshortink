#!/usr/bin/env python3
"""inshorturl_http.py — resolve inshorturl.in shortlinks with NO browser at all.

Every hop is a plain HTTP request. The two Cloudflare gates are solved
out-of-process by the cfast pure-protocol solver (Rust VM, no browser):

  cf_clearance  : cfast's `challenge` binary replays the managed challenge and
                  prints `cf_clearance=...` (rebuilt from source; see notes).
  turnstile     : cfast's solver API answers the /links/go widget.

Flow (all HTTP, measured end to end):

  1. GET  inshorturl.in/<alias>          -- 403 CF managed challenge
     cfast challenge --page <url> --solve
       -> cf_clearance (bound to the profile UA + egress IP)
  2. GET  inshorturl.in/<alias> with that cookie + a mahitimanch referer
       -> 307 toolkit.inshorturl.com/check/?link=<alias>&id=<N>   (or the
          verify page directly, when the referer is a completed filler page)
  3. GET  mahitimanch.in/folder.php?link=<alias>&id=<N>
       sets cookies any=<alias>, page=<N>; JS redirect (via google.com/url)
       to a filler article
  4. POST the four step forms (any1 / any2+page_value / any3+verification_token
       / get) -- self-posts, no timer, ~0.7s each
       -> final filler page with <a id="gtelinkbtn" href="https://inshorturl.com/<alias>">
  5. GET  inshorturl.in/<alias> with Referer = that filler page
       -> the verify page: #timer + turnstile sitekey + POST /links/go form
  6. POST solver /solve (sitekey) -> token
     POST inshorturl.in/links/go (_csrfToken, ad_form_data, cf_turnstile_response)
       -> {"status":"success","url":"<destination>"}

Why a mobile (iOS Safari) UA everywhere: the cfast profile is iOS Safari 26.2,
and cf_clearance is bound to the UA that solved it. mahitimanch accepts any
non-Firefox UA, so one client can do everything.

Requirements:
  - cfast solver binary + profile installed under /opt/cfast (setup.sh)
  - solver API on localhost:8810 (cfast.service)
  - httpx[http2]

Usage:
    python3 inshorturl_http.py https://inshorturl.in/Tddfjl8
    python3 inshorturl_http.py Tddfjl8 --json
    python3 inshorturl_http.py Tddfjl8 --verbose
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import httpx

ENTRY = "https://inshorturl.in"
BIN = os.environ.get("CFast_BIN", "/opt/cfast/challenge")
PROFILE = os.environ.get("CFast_PROFILE", "/opt/cfast/profile_safari.json")
API = os.environ.get("CFast_API", "http://localhost:8810")
UA = os.environ.get(
    "Inshort_UA",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.3 Mobile/15E148 "
    "Safari/604.1",
)

STEP_RE = re.compile(r"You Are On<span>\s*Step\s*(\d+)")
FORM_RE = re.compile(
    r'<form id="tpForm"[^>]*action="([^"]*)"[^>]*>(.*?)</form>', re.S
)
INPUT_RE = re.compile(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
GETLINK_RE = re.compile(r'<a[^>]*id="gtelinkbtn"[^>]*href="([^"]+)"')
GOFORM_RE = re.compile(r'<form[^>]*id="ts-go-link".*?</form>', re.S)


def log(verbose, *a):
    if verbose:
        print("[http]", *a, file=sys.stderr, flush=True)


def alias_of(link):
    if "://" not in link:
        return link.strip("/")
    path = urllib.parse.urlparse(link).path.strip("/")
    if not path:
        raise SystemExit(f"no alias in {link!r}")
    return path.split("/")[0]


def js_redirect(html):
    m = re.search(r"redirectUrl\s*=\s*['\"]([^'\"]+)['\"]", html)
    if m:
        return m.group(1)
    m = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']', html)
    if m:
        u = m.group(1)
        if "google.com/url" in u:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            u = q.get("url", [u])[0]
        return u
    return None


def parse_form(html):
    m = FORM_RE.search(html)
    if not m:
        return None, {}
    return m.group(1), dict(INPUT_RE.findall(m.group(2)))


# ── Cloudflare gates ─────────────────────────────────────────────────

def solve_clearance(alias, verbose=False, timeout=90, attempts=3):
    """Run cfast's challenge binary; return the cf_clearance cookie value.

    The binary prints `cf_clearance=<value>` on success. It binds the cookie to
    its own egress IP and profile UA, which is why this script reuses both.
    Occasionally a probe collector is not recognized; the challenge script
    rotates per request, so retrying usually succeeds.
    """
    url = f"{ENTRY}/{alias}"
    last = ""
    for attempt in range(1, attempts + 1):
        with __import__("tempfile").TemporaryDirectory(prefix="cfast-") as out:
            cmd = [
                BIN, "--page", url, out, "--solve",
                "--fingerprint", PROFILE,
            ]
            log(verbose, f"cfast (try {attempt}):", " ".join(cmd))
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("cf_clearance="):
                return line.split("=", 1)[1].strip()
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = tail[-1][:160] if tail else "no output"
        log(verbose, f"  failed: {last}")
    raise RuntimeError(f"cfast did not return cf_clearance: {last}")


def solve_turnstile(url, sitekey, verbose=False, timeout=150):
    """Ask the cfast HTTP API for a Turnstile token bound to `url`'s sitekey."""
    body = {"url": url, "sitekey": sitekey, "timeout": timeout - 10}
    try:
        r = httpx.post(f"{API}/solve", json=body, timeout=timeout)
        data = r.json()
    except Exception as exc:
        raise RuntimeError(f"solver API unreachable: {exc}") from exc
    if data.get("status") != 1 or not data.get("token"):
        raise RuntimeError(f"solver refused: {data}")
    log(verbose, f"turnstile token len={len(data['token'])}")
    return data["token"]


# ── shortlink chain ──────────────────────────────────────────────────

def walk_mahitimanch(client, alias, verbose=False):
    """folder.php -> filler article -> 4 self-posts -> final filler page.

    Returns the final page URL (its referer unlocks the verify page later).
    """
    r = client.get(
        f"https://mahitimanch.in/folder.php?link={alias}&id=2",
        headers={"Referer": "https://toolkit.inshorturl.com/"},
    )
    hop = js_redirect(r.text)
    if not hop:
        raise RuntimeError(f"folder.php gave no redirect ({r.status_code})")
    log(verbose, "folder ->", hop)
    r = client.get(hop, headers={"Referer": f"https://mahitimanch.in/folder.php?link={alias}&id=2"})

    # any=<alias> + page=<n> cookies are what make the step forms render.
    for i in range(6):
        action, fields = parse_form(r.text)
        step = STEP_RE.search(r.text)
        log(verbose, f"step {step.group(1) if step else '?'}: post {action or '(self)'}")
        if action is None:
            break
        # Empty hidden fields are filled from the session state the browser holds.
        if "any2" in fields and not fields.get("any2"):
            fields["any2"] = alias
        if "page_value" in fields and not fields.get("page_value"):
            fields["page_value"] = client.cookies.get("page", "2")
        if "any3" in fields and not fields.get("any3"):
            fields["any3"] = alias
        r = client.post(
            action or str(r.url), data=fields, headers={"Referer": str(r.url)}
        )
        hop = js_redirect(r.text)
        if hop:
            r = client.get(hop, headers={"Referer": str(r.url)})
    if "gtelinkbtn" not in r.text:
        raise RuntimeError("never reached the Get Link page")
    return str(r.url)


def resolve(alias, verbose=False):
    client = httpx.Client(
        http2=True,
        timeout=30,
        follow_redirects=False,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        t0 = time.monotonic()
        clearance = solve_clearance(alias, verbose)
        log(verbose, f"cf_clearance len={len(clearance)} ({time.monotonic()-t0:.1f}s)")
        client.cookies.set("cf_clearance", clearance, domain="inshorturl.in")

        filler = walk_mahitimanch(client, alias, verbose)
        log(verbose, "filler:", filler)

        # The verify page is served when the request carries the filler page as
        # its referer — that is what clicking Get Link does.
        r = client.get(f"{ENTRY}/{alias}", headers={"Referer": filler})
        if "cf-turnstile" not in r.text:
            raise RuntimeError(
                f"verify page not served ({r.status_code}, len {len(r.text)})"
            )
        log(verbose, "verify page ok")

        form = GOFORM_RE.search(r.text)
        if not form:
            raise RuntimeError("no /links/go form on the verify page")
        block = form.group(0)
        action = re.search(r'action="([^"]+)"', block).group(1)
        csrf = re.search(r'name="_csrfToken"[^>]*value="([^"]+)"', block).group(1)
        adata = re.search(r'name="ad_form_data"[^>]*value="([^"]+)"', block).group(1)
        sitekey = re.search(r'data-sitekey="([^"]+)"', r.text).group(1)

        token = solve_turnstile(f"{ENTRY}/{alias}", sitekey, verbose)

        r2 = client.post(
            f"{ENTRY}{action}",
            data={
                "_method": "POST",
                "_csrfToken": csrf,
                "ad_form_data": adata,
                "cf_turnstile_response": token,
            },
            headers={
                "Referer": str(r.url),
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        data = r2.json()
        if data.get("status") != "success" or not data.get("url"):
            raise RuntimeError(f"/links/go refused: {data}")
        return data["url"]
    finally:
        client.close()


def main():
    ap = argparse.ArgumentParser(
        description="Resolve inshorturl.in shortlinks with pure HTTP (no browser)."
    )
    ap.add_argument("link", help="short link or bare alias")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--verbose", action="store_true", help="log each hop")
    args = ap.parse_args()

    try:
        url = resolve(alias_of(args.link), args.verbose)
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"short": args.link, "destination": url}, indent=2))
    else:
        print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())