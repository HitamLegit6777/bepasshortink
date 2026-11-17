#!/usr/bin/env python3
"""inshorturl_bypass.py — resolve an inshorturl.in shortlink to its destination.

Measured flow for https://inshorturl.in/<alias> (2026-11):

  inshorturl.in/<alias>            Cloudflare *managed* challenge (cf-mitigated)
    -> 307 toolkit.inshorturl.com/check/?link=<alias>&id=<N>
    -> <a data-web-btn> mahitimanch.in/folder.php?link=<alias>&id=<N>
         sets cookies any=<alias>, page=<N>; JS redirect (through google.com/url)
         to a filler article
    -> Step 1/4 .. 4/4: each page renders one hidden field and POSTs it back
         (any1 / any2+page_value / any3+verification_token / get)
         The *final* page only exists as the POST response to step 4 — a plain
         GET of that URL renders Step 1 again, so progress lives server-side,
         not in the cookies.
    -> final page: <a id="gtelinkbtn" href="https://inshorturl.com/<alias>">
    -> clicking it 301s to inshorturl.in/<alias>, which is NOT the entry page but
         a verify page: #timer countdown + Cloudflare Turnstile
         (sitekey 0x4AAAAAADVAQhrsWIJKBKAb), then POST /links/go
         -> {"status":"success","url":"<real destination>"}

Why this needs a browser end-to-end:
  * the managed challenge cannot be bypassed with a replayed cf_clearance —
    the cookie is bound to Camoufox's TLS/JA3 fingerprint and exact UA. Lifting
    it into curl_cffi returns 403 for every TLS profile (firefox/chrome/safari).
  * the last hop (inshorturl.in verify page) is behind that same challenge, and
    it is the hop that carries the destination.
The 4 filler steps are pure HTTP-able (proven), but their result cannot be
handed to the browser: the final page is POST-response-only, so the browser
would start over at Step 1. Doing them in the browser is therefore both
cheaper and correct — and the 10s "Human Verify" timer can be skipped in-page.

This is the fallback lane. inshorturl_http.py does the whole chain with plain
HTTP (cfast answers both Cloudflare gates) in ~20-35 s and needs no browser;
use this one when cfast is unavailable or when debugging a changed flow.

Usage:
    python3 inshorturl_bypass.py https://inshorturl.in/Tddfjl8
    python3 inshorturl_bypass.py Tddfjl8 --json
    python3 inshorturl_bypass.py Tddfjl8 --proxy socks5://127.0.0.1:1600
    python3 inshorturl_bypass.py Tddfjl8 --headed      # watch it run
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.parse

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
ENTRY_HOST = "inshorturl.in"
SKIP_TURNSTILE_CLICK = os.environ.get("INSHORT_SKIP_CLICK") == "1"

# Same Camoufox preference set cf_solver.py uses to clear this site's challenge.
_FIREFOX_PREFS = {
    "webgl.disabled": True,
    "webgl.force-enabled": False,
    "webgl.enable-webgl2": False,
    "gfx.webrender.all": False,
    "layers.acceleration.disabled": True,
    "media.peerconnection.enabled": False,
    "dom.webdriver.enabled": False,
    "useAutomationExtension": False,
    "privacy.resistFingerprinting": False,
    "canvas.poisondata": False,
    "security.csp.enable": False,
    "security.data_uri.block_toplevel_data_uri_navigations": False,
}

WEB_RE = re.compile(r'var WEB\s*=\s*"([^"]+)"')
STEP_RE = re.compile(r"Step\s*(\d+)\s*/\s*(\d+)")
FORM_RE = re.compile(r'<form id="tpForm"', re.S)


_T0 = time.monotonic()


def log(*a):
    print(f"[{time.monotonic() - _T0:6.1f}s]", *a, file=sys.stderr, flush=True)


def alias_of(link):
    """'https://inshorturl.in/Tddfjl8' -> 'Tddfjl8'."""
    if "://" not in link:
        return link.strip("/")
    path = urllib.parse.urlparse(link).path.strip("/")
    if not path:
        raise SystemExit(f"no alias in {link!r}")
    return path.split("/")[0]


async def _new_page(proxy=None, headed=False):
    from camoufox.async_api import AsyncCamoufox

    kw = {"headless": not (headed or bool(os.environ.get("DISPLAY"))),
          "humanize": True,
          "firefox_user_prefs": _FIREFOX_PREFS}
    if proxy:
        kw["proxy"] = {"server": proxy}
        kw["geoip"] = True
    browser = await AsyncCamoufox(**kw).__aenter__()
    ctx = await browser.new_context()

    async def route(r):
        # mahitimanch.in answers Firefox with "Open Link In Chrome". The UA is
        # overridden per-request only for that host: inshorturl must keep the
        # native UA or its cf_clearance stops matching.
        if "mahitimanch.in" in r.request.url:
            h = dict(r.request.headers)
            h["user-agent"] = CHROME_UA
            await r.continue_(headers=h)
        else:
            await r.continue_()

    await ctx.route("**/*", route)
    return browser, ctx, await ctx.new_page()


async def _wait_off_challenge(page, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.75)
        try:
            if "just a moment" not in (await page.title()).lower():
                return
        except Exception:
            continue
    raise TimeoutError("cloudflare managed challenge did not clear")


async def _advance_steps(page, max_submits=24):
    """Walk Step 1/4..4/4 by submitting the step form directly.

    The 10s "Human Verify" countdown is pure theatre: the server advances on the
    POST alone, so clicking #link + waiting out the timer only burns ~10s per
    step. Submitting #tpForm reaches the Get Link page in ~16s for all four.
    The server sometimes re-serves the same step, so retry until the tag moves.
    """
    last = None
    stall = 0
    for _ in range(max_submits):
        try:
            html = await page.content()
        except Exception:            # mid-navigation
            await asyncio.sleep(0.8)
            continue
        if "gtelinkbtn" in html:
            return True
        m = STEP_RE.search(html)
        tag = m.group(0) if m else None
        if not FORM_RE.search(html):
            await asyncio.sleep(0.8)
            continue
        if tag == last:
            stall += 1
            if stall > 6:
                raise RuntimeError(f"stuck on {tag}")
        else:
            stall = 0
            last = tag
            log(f"at {tag}")
        await page.evaluate(
            "() => document.getElementById('tpForm').submit()")
        await asyncio.sleep(1.6)
    return "gtelinkbtn" in await page.content()


async def _solve_turnstile(page):
    """Click the inline widget, then poll the token the widget writes."""
    for _ in range(2):
        try:
            await page.click(".cf-turnstile", timeout=4000)
            break
        except Exception:
            await asyncio.sleep(1.5)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            tok = await page.evaluate(
                "() => (document.querySelector('[name=cf-turnstile-response]')"
                " || {}).value || ''")
        except Exception:
            tok = ""
        if tok and len(tok) > 40:
            log("turnstile token acquired")
            return tok
        await asyncio.sleep(0.4)
    return ""


async def _resolve(alias, proxy=None, headed=False, timeout=120):
    browser, ctx, page = await _new_page(proxy, headed)
    try:
        await page.goto(f"https://{ENTRY_HOST}/{alias}",
                        wait_until="domcontentloaded", timeout=45000)
        await _wait_off_challenge(page)
        log("challenge cleared ->", page.url)

        # check page -> "Continue on web"
        web = WEB_RE.search(await page.content())
        log("check page WEB ->", web.group(1) if web else "<none>")
        await page.click("[data-web-btn]", timeout=15000)
        for _ in range(25):
            await asyncio.sleep(1)
            if "mahitimanch.in" in page.url:
                break
        log("filler host ->", page.url)

        if not await _advance_steps(page):
            raise RuntimeError("never reached the Get Link page")
        href = await page.get_attribute("#gtelinkbtn", "href")
        log("get link ->", href)
        if not href:
            raise RuntimeError("Get Link button has no href")

        # Read /links/go. The verify page must be reached by clicking: a direct
        # navigation to the same URL is bounced back to the check page.
        box = {}

        async def on_resp(r):
            if "/links/go" in r.url:
                try:
                    box["body"] = await r.text()
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))
        # The button sits behind a 5s "Please wait" countdown; wait for it to
        # be live instead of a fixed sleep.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if await page.evaluate(
                        "() => { const b = document.getElementById('gtelinkbtn');"
                        " return b && b.offsetParent !== null; }"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await page.click("#gtelinkbtn", timeout=15000)
        # 301 inshorturl.com -> inshorturl.in verify page; wait for that host
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            await asyncio.sleep(0.8)
            try:
                if ENTRY_HOST in page.url:
                    break
            except Exception:
                pass
        log("verify page ->", page.url)
        if not SKIP_TURNSTILE_CLICK:
            await _solve_turnstile(page)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and "body" not in box:
            await asyncio.sleep(1.5)
        raw = box.get("body")
        if not raw:
            raise TimeoutError("/links/go never fired (turnstile unsolved?)")
        try:
            data = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"/links/go returned non-JSON: {raw[:200]}")
        if data.get("status") != "success" or not data.get("url"):
            raise RuntimeError(f"/links/go refused: {data}")
        return data["url"]
    finally:
        try:
            await browser.__aexit__(None, None, None)
        except Exception:
            pass


def resolve(link, proxy=None, headed=False, timeout=120):
    """Blocking API: return the destination URL for a short link or alias."""
    return asyncio.run(_resolve(alias_of(link), proxy, headed, timeout))


def main():
    ap = argparse.ArgumentParser(
        description="Resolve an inshorturl.in shortlink to its destination.")
    ap.add_argument("link", help="short link or bare alias")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--proxy", help="http(s)/socks5 proxy for the browser")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--timeout", type=int, default=120,
                    help="seconds to wait for /links/go (default 120)")
    args = ap.parse_args()

    try:
        url = resolve(args.link, args.proxy, args.headed, args.timeout)
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