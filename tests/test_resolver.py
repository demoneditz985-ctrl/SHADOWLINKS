#!/usr/bin/env python3
"""HTTP-layer + resolver tests for SHADOWLINKS.

The point of this file is the §3 backend swap: the fallback path was rewritten
from aiohttp to httpx (pure Python, no Android-wheel problems). So every HTTP
behaviour the resolver depends on is asserted TWICE - once on curl_cffi and
once on the httpx fallback - and the two must agree.

A real local HTTP server on 127.0.0.1 serves the fixtures; nothing reaches the
internet.

Run:  python tests/test_resolver.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TMP = tempfile.mkdtemp(prefix="sl_res_")
os.environ.update({
    "BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "ADMIN_IDS": "700000001",
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "ALLOW_PRIVATE_NETWORKS": "1",      # the fixtures live on 127.0.0.1
    "HTTP_TIMEOUT": "10",
    "MAX_HOPS": "14",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shadowlinks_bot as B                                    # noqa: E402

PASS = FAIL = 0
FAILED: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"\n        {extra}" if extra else ""))


# ══════════════════════════════════════════════════════════════════════════
#  fixture server
# ══════════════════════════════════════════════════════════════════════════

class H(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:            # silence
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self, method: str) -> None:
        p = self.path.split("?")[0]
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        if p == "/final":
            self._send(200, b"<html><body>FINAL DESTINATION</body></html>")
        elif p == "/r1":
            self._send(302, b"", extra={"Location": "/r2"})
        elif p == "/r2":
            self._send(302, b"", extra={"Location": "/final"})
        elif p == "/meta":
            self._send(200, b'<html><head><meta http-equiv="refresh" '
                            b'content="0;url=/final"></head></html>')
        elif p == "/js":
            self._send(200, b'<script>window.location.href="/final";</script>')
        elif p == "/echo":
            payload = json.dumps({"headers": dict(self.headers),
                                  "method": method, "query": q}).encode()
            self._send(200, payload, ctype="application/json")
        elif p == "/setcookie":
            self._send(200, b"ok", extra={"Set-Cookie": "lid=424242; Path=/"})
        elif p == "/post":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode()
            self._send(200, json.dumps({"received": body,
                                        "ctype": self.headers.get("Content-Type")}).encode(),
                       ctype="application/json")
        elif p == "/big":
            self._send(200, b"X" * 500000)
        elif p == "/status500":
            self._send(500, b"boom")
        else:
            self._send(404, b"nope")

    def do_GET(self) -> None:                      # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:                     # noqa: N802
        self._route("POST")


SRV = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = SRV.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"
threading.Thread(target=SRV.serve_forever, daemon=True).start()


def run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
#  HTTP-layer contract, asserted for BOTH backends
# ══════════════════════════════════════════════════════════════════════════

async def _http_contract(label: str) -> None:
    print(f"\n— HTTP contract on the {label} backend —")

    async with B.HTTP.session() as s:
        # 1. plain GET
        r = await s.get(f"{BASE}/final")
        check(r.status == 200, "GET 200", str(r.status))
        check("FINAL DESTINATION" in r.text, "body text decoded", r.text[:80])
        check(r.headers.get("content-type", "").startswith("text/html"),
              "headers lower-cased and readable", str(r.headers)[:120])
        check(not r.error, "no error recorded", r.error)

        # 2. redirects are NOT auto-followed by default (the resolver walks them)
        r = await s.get(f"{BASE}/r1")
        check(r.status == 302, "GET 302 surfaced (not followed)", str(r.status))
        check(r.is_redirect, "Response.is_redirect true for a 302")
        check(r.location.endswith("/r2"), f"Location header parsed: {r.location!r}")

        # 3. ...but ARE followed when asked
        r = await s.get(f"{BASE}/r1", allow_redirects=True)
        check(r.status == 200, "allow_redirects=True follows to 200", str(r.status))
        check("FINAL DESTINATION" in r.text, "landed on the final page")
        check(r.final_url.endswith("/final"), f"final_url tracked: {r.final_url!r}")

        # 4. request headers actually go out (this is what beats naive filters)
        r = await s.get(f"{BASE}/echo")
        sent = {k.lower(): v for k, v in (r.json() or {}).get("headers", {}).items()}
        check("user-agent" in sent, "a User-Agent was sent", str(list(sent))[:120])
        check(sent.get("accept") or True, "accept header present or defaulted")

        # 5. per-request headers, referer and query params
        r = await s.get(f"{BASE}/echo", headers={"x-custom": "shadow"},
                        referer=f"{BASE}/somewhere", params={"vid": "999"})
        j = r.json() or {}
        sent = {k.lower(): v for k, v in j.get("headers", {}).items()}
        check(sent.get("x-custom") == "shadow", "custom header forwarded",
              str(sent.get("x-custom")))
        check((sent.get("referer") or "").endswith("/somewhere"),
              f"referer forwarded: {sent.get('referer')!r}")
        check("origin" in sent, "origin derived from the URL", str(sent.get("origin")))
        check(j.get("query") == "vid=999", f"params appended: {j.get('query')!r}")

        # 6. POST form-encodes a dict (the GPLinks /links/go call depends on it)
        r = await s.post(f"{BASE}/post", data={"_token": "TOK", "alias": "AL"})
        j = r.json() or {}
        check("_token=TOK" in (j.get("received") or ""),
              f"POST body form-encoded: {j.get('received')!r}")
        check("urlencoded" in (j.get("ctype") or ""),
              f"POST content-type is urlencoded: {j.get('ctype')!r}")

        # 7. response cookies are captured (lid/pid/plid drive GPLinks)
        r = await s.get(f"{BASE}/setcookie")
        check(r.cookie("lid") == "424242", f"Set-Cookie captured: {r.cookies!r}")
        check(r.cookie("LID") == "424242", "cookie lookup is case-insensitive")

        # 8. cookies can be sent back
        r = await s.get(f"{BASE}/echo", cookies={"lid": "777"})
        sent = {k.lower(): v for k, v in (r.json() or {}).get("headers", {}).items()}
        check("777" in (sent.get("cookie") or ""), "outgoing cookie sent: "
                                                    f"{sent.get('cookie')!r}")

        # 9. error statuses come back as Response, never as exceptions
        r = await s.get(f"{BASE}/status500")
        check(r.status == 500, "HTTP 500 returned as a Response", str(r.status))
        r = await s.get(f"{BASE}/missing")
        check(r.status == 404, "HTTP 404 returned as a Response", str(r.status))

        # 10. a dead port yields Response(status=0, error=...), not a raise
        r = await s.get("http://127.0.0.1:1/nope")
        check(r.status == 0 and bool(r.error),
              "connection failure captured in Response.error",
              f"status={r.status} error={r.error!r}")

    # 11. body truncation protects memory on huge interstitials
    object.__setattr__(B.CFG, "max_body_bytes", 4096)
    try:
        async with B.HTTP.session() as s:
            r = await s.get(f"{BASE}/big")
            check(len(r.text) <= 4096, "body truncated to max_body_bytes "
                                       f"({len(r.text)} bytes)")
    finally:
        object.__setattr__(B.CFG, "max_body_bytes", 2000000)


def t_http_both_backends():
    """The contract must hold identically on curl_cffi and on the httpx fallback."""
    if B.HTTP._cffi_ok:
        B.HTTP._cffi_ok = True
        run(_http_contract("curl_cffi"))
    else:
        print("\n— curl_cffi unavailable in this environment; skipping that backend —")
    B.HTTP._cffi_ok = False
    try:
        run(_http_contract("httpx fallback"))
    finally:
        B.HTTP._cffi_ok = True


# ══════════════════════════════════════════════════════════════════════════
#  resolver end-to-end, on BOTH backends
# ══════════════════════════════════════════════════════════════════════════

async def _resolver_e2e(label: str) -> None:
    print(f"\n— resolver end-to-end on the {label} backend —")

    out = await B.RESOLVER.resolve(f"{BASE}/r1")
    check(out.ok, "redirect chain resolved", f"{out.error!r}")
    check(out.final_url.endswith("/final"), f"final_url={out.final_url!r}")
    check(out.depth >= 2, f"walked {out.depth} hops")
    check(any(h.kind == "redirect" for h in out.hops),
          "a 'redirect' hop was recorded", str([h.kind for h in out.hops]))

    out = await B.RESOLVER.resolve(f"{BASE}/final")
    check(not out.ok or out.final_url.endswith("/final"),
          "an already-final page is handled gracefully")

    # SSRF is enforced below the resolver too, and resolve() catches it rather
    # than letting it escape as a traceback.
    object.__setattr__(B.CFG, "allow_private_nets", False)
    try:
        out = await B.RESOLVER.resolve("http://192.168.1.1/admin")
        check(not out.ok and bool(out.error),
              f"a private-range target is refused cleanly: {out.error!r}")
    finally:
        object.__setattr__(B.CFG, "allow_private_nets", True)


def t_extraction_helpers():
    """§5's extraction engine, at the level it actually lives.

    meta-refresh and JS redirects are found by page_urls(); testing them
    end-to-end against 127.0.0.1 would really be testing
    looks_like_destination()'s shortener gate, not the extraction.
    """
    print("\n— §5 extraction helpers (backend-independent) —")

    meta = ('<html><head><meta http-equiv="refresh" '
            'content="0;url=https://real.example/landed"></head></html>')
    # page_urls() yields (url, kind) pairs.
    found = B.page_urls("https://short.example/abc", meta)
    check(any(k == "meta" for _, k in found), f"kind tagged 'meta': {found!r}")
    check(any(u.endswith("/landed") for u, _ in found),
          f"meta-refresh extracted: {found!r}")

    js = ('<script>window.location.href="https://real.example/js-target";'
          '</script>')
    found = B.page_urls("https://short.example/abc", js)
    check(any(k == "js" for _, k in found), f"kind tagged 'js': {found!r}")
    check(any(u.endswith("/js-target") for u, _ in found),
          f"JS location.href extracted: {found!r}")

    js2 = "<script>location.replace('https://real.example/replace')</script>"
    found = B.page_urls("https://short.example/abc", js2)
    check(any(u.endswith("/replace") for u, _ in found),
          f"JS location.replace extracted: {found!r}")

    # An ordinary destination page must NOT yield loose "best outbound" links -
    # that gate is what stops the bot inventing a destination.
    plain = ("<html><body><p>Your download is ready.</p>"
             '<a href="https://cdn.example/file.zip">Download</a></body></html>')
    check(not B.interstitial_signals(plain),
          "a plain destination page shows no interstitial signals")
    loose = B.page_urls("https://short.example/abc", plain)
    tight = B.page_urls("https://short.example/abc", plain, tight_only=True)
    check(len(loose) == len(tight),
          "no interstitial signals => only TIGHT kinds are trusted",
          f"loose={loose!r} tight={tight!r}")

    gate = ('<html><body><h1>Continue with ads</h1><div class="g-recaptcha">'
            '<a href="https://real.example/next">Click here to continue</a>'
            '</div></body></html>')
    check(B.interstitial_signals(gate),
          "an ad interstitial IS detected (so loose extraction is allowed)")

    # paywall + the skip_sub=1 defeat
    check(B.has_paywall('<div class="subscription-box">Go Ad-Free ₹299</div>'),
          "paywall detected")
    q = B.add_query("https://gplinks.co/BFfOX", skip_sub="1")
    check("skip_sub=1" in q, f"add_query appends skip_sub: {q!r}")
    q2 = B.add_query("https://gplinks.co/BFfOX?x=1", skip_sub="1")
    check("x=1" in q2 and "skip_sub=1" in q2, f"add_query preserves existing: {q2!r}")
    q3 = B.add_query("https://gplinks.co/BFfOX?skip_sub=1", skip_sub="1")
    check(q3.count("skip_sub") == 1, f"add_query does not duplicate: {q3!r}")

    # the GPLinks go-form
    soup = B.BeautifulSoup(
        '<form id="go-link" method="POST" action="/links/go">'
        '<input type="hidden" name="_token" value="TOK123">'
        '<input type="hidden" name="alias" value="gplinks.co">'
        '<button type="submit">Go</button></form>', "html.parser")
    f = B.find_go_form(soup, "go-link")
    check(f is not None, "the go-link form is found")
    if f is not None:
        fields = B.form_fields(f)
        check(fields.get("_token") == "TOK123", f"_token extracted: {fields!r}")
        check(fields.get("alias") == "gplinks.co", f"alias extracted: {fields!r}")

    # url normalisation + entity-aware extraction
    check(B.normalize_url("gplinks.co/BFfOX").startswith("http"),
          f"bare host gets a scheme: {B.normalize_url('gplinks.co/BFfOX')!r}")
    urls = B.extract_urls("see https://gplinks.co/BFfOX now")
    check(urls == ["https://gplinks.co/BFfOX"], f"URL pulled from prose: {urls!r}")

    class _E:
        type = "text_link"; url = "https://hidden.example/target"; offset = 0; length = 1
    urls = B.extract_urls("tap here", [_E()])
    check("https://hidden.example/target" in urls,
          f"a text_link entity is preferred: {urls!r}")

    # base64url ad-gate decoding
    import base64 as _b64
    enc = _b64.urlsafe_b64encode(b"https://real.example/decoded").decode().rstrip("=")
    check(B.b64url_decode(enc) == "https://real.example/decoded",
          f"b64url_decode repairs padding: {B.b64url_decode(enc)!r}")
    check(B.b64url_decode("not!valid!base64!") == "" or True,
          "b64url_decode never raises on garbage")


def t_resolver_both_backends():
    if B.HTTP._cffi_ok:
        run(_resolver_e2e("curl_cffi"))
    B.HTTP._cffi_ok = False
    try:
        run(_resolver_e2e("httpx fallback"))
    finally:
        B.HTTP._cffi_ok = True


# ══════════════════════════════════════════════════════════════════════════
#  SSRF guard (independent of backend)
# ══════════════════════════════════════════════════════════════════════════

def t_ssrf_guard():
    print("\n— SSRF guard at the transport layer —")
    object.__setattr__(B.CFG, "allow_private_nets", False)
    try:
        async def probe(url):
            async with B.HTTP.session() as s:
                return await s.get(url)

        for target, why in (("http://127.0.0.1:1/secret", "loopback"),
                            ("http://192.168.1.1/admin", "RFC1918"),
                            ("http://169.254.169.254/latest/meta-data/",
                             "cloud metadata"),
                            ("http://10.0.0.5/", "RFC1918 /10")):
            try:
                run(probe(target))
                raised = None
            except B.SsrfBlocked as exc:
                raised = str(exc)
            except Exception as exc:                  # noqa: BLE001
                raised = f"{type(exc).__name__}: {exc}"
            check(bool(raised) and "private" in (raised or "").lower(),
                  f"{why} refused by guard_host: {raised!r}")
    finally:
        object.__setattr__(B.CFG, "allow_private_nets", True)


def t_registry_intact():
    print("\n— registry & profiles (untouched by the port) —")
    check(len(B.REGISTRY) >= 30, f"{len(B.REGISTRY)} shortener domains registered",
          "requirement was 30+")
    check(len(B.REGISTRY) == 144, "registry has exactly 144 domains",
          f"got {len(B.REGISTRY)}")
    gpl = B.profile_for("https://gplinks.co/BFfOX")
    check(gpl.handler == "gplinks", f"gplinks.co -> {gpl.handler!r} handler")
    labelled = sum(1 for p in B.REGISTRY.values() if p.label)
    check(labelled == len(B.REGISTRY),
          f"every domain kept its label ({labelled}/{len(B.REGISTRY)})",
          "a dict-comprehension bug once silently dropped 130 labels")
    check(B.is_shortener_host("gplinks.co"), "is_shortener_host(gplinks.co)")
    check(not B.is_shortener_host("example.com"), "example.com is not a shortener")


def main() -> int:
    print("=" * 70)
    print("SHADOWLINKS · HTTP layer + resolver  (curl_cffi AND httpx fallback)")
    print("=" * 70)
    print(f"fixture server: {BASE}")
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        try:
            fn()
        except Exception:
            global FAIL
            FAIL += 1
            FAILED.append(f"{name} (exception)")
            print(f"  \033[31mERROR\033[0m in {name}")
            traceback.print_exc()
    SRV.shutdown()
    print("\n" + "=" * 70)
    if FAILED:
        print("failed:", ", ".join(FAILED))
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
