#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end tests for solver_api.py over REAL HTTP.

Nothing reaches the internet. A local ThreadingHTTPServer on 127.0.0.1 serves
the shortener fixtures, and FastAPI's TestClient drives the API in-process
through the full ASGI stack - middleware, dependencies, validation, handlers.

The fixtures deliberately use two hostnames for one server:
    127.0.0.1  = the "shortener"
    localhost  = the "destination"
so a resolved chain ends on a host that is not in the registry, which is what
makes ResolveOutcome.ok true. Registering 127.0.0.1 as a shortener instead
would make the resolver believe it never escaped, and ok would be False.

Run:  python tests/test_api.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = secrets.token_urlsafe(48)
ADMIN = "admin-test-secret-value"

os.environ.update({
    "AUTH_SECRET": SECRET,
    "ADMIN_SECRET": ADMIN,
    "ALLOW_PRIVATE_NETWORKS": "1",   # the fixtures live on 127.0.0.1
    "HTTP_TIMEOUT": "10",
    "SOLVE_TIMEOUT": "45",
    "CACHE_TTL": "300",
    "CACHE_MAX": "500",
    "MAX_CONCURRENCY": "4",
    "GOLINK_DELAY": "0.1",
    "RATE_RPM": "100000",            # per-key limits are tested explicitly
    "RATE_BURST": "0",
    "BATCH_LIMIT": "25",
    "LOG_LEVEL": "CRITICAL",
})

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import solver_api as A                                    # noqa: E402
from fastapi.testclient import TestClient                 # noqa: E402

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
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   <- {extra}" if extra else ""))


# ══════════════════════════════════════════════════════════════════════════
# fixture shortener
# ══════════════════════════════════════════════════════════════════════════

class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:      # silence
        pass

    def _send(self, code: int, body: str = "", ctype: str = "text/html",
              headers: dict | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self) -> None:               # noqa: N802
        p = self.path.split("?")[0]
        port = self.server.server_address[1]
        dest = f"http://localhost:{port}/done"

        if p == "/done":
            self._send(200, "<html><body><h1>the real destination</h1>"
                            "<p>download ready</p></body></html>")
        elif p == "/go/hop1":
            self._send(302, "", headers={"Location": "/go/hop2"})
        elif p == "/go/hop2":
            self._send(302, "", headers={"Location": dest})
        elif p == "/go/meta":
            self._send(200, '<html><head><meta http-equiv="refresh" '
                            f'content="0;url={dest}"></head>'
                            "<body>please wait, verifying you are human</body></html>")
        elif p == "/go/captcha":
            # Reproduces, offline, the exact page a live test against a
            # DataDome-protected host returned: a bot-challenge interstitial
            # with a captcha URL sitting inside an embedded JSON "url" field.
            # The resolver used to report that captcha as the destination
            # with ok:true.
            self._send(200,
                       '<html><head><script type="text/javascript">'
                       'var cfg = {"url":"http://geo.captcha-delivery.com/'
                       'captcha/?initialCid=AHrlqA&cid=pI9821&hash=2CF892"};'
                       '</script></head><body>'
                       '<h1>Verify you are human</h1>'
                       '<p>Please complete the security check.</p>'
                       '</body></html>')
        elif p == "/go/dead":
            self._send(404, "<html>not found</html>")
        elif p == "/go/selfloop":
            self._send(302, "", headers={"Location": "/go/selfloop"})
        elif p == "/go/external":
            # A hop to a host that will not resolve: exercises the error path.
            self._send(302, "", headers={
                "Location": "http://nonexistent.invalid.example/x"})
        else:
            self._send(404, "<html>unknown fixture</html>")


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
DEST = f"http://localhost:{PORT}/done"

CLIENT = TestClient(A.app, raise_server_exceptions=False)


def key(**kw) -> str:
    kw.setdefault("secret", SECRET)
    return A.mint_key(**kw)[0]


VIP = key(name="vip-tester", tier="vip", days=30)
FREE = key(name="free-tester", tier="free", days=30)
HDR = {"X-API-Key": VIP}


# ══════════════════════════════════════════════════════════════════════════
print("\n— unauthenticated surface —")
# ══════════════════════════════════════════════════════════════════════════

r = CLIENT.get("/health")
check(r.status_code == 200, "GET /health needs no key (UptimeRobot target)",
      str(r.status_code))
check(r.json().get("status") == "ok", "/health reports ok", r.text[:120])
check("X-API-Key" not in json.dumps(r.json()), "/health leaks no config")

r = CLIENT.get("/")
check(r.status_code == 200, "GET / needs no key")
info = r.json()
check(info.get("domains") == len(A.REGISTRY), "/ reports the registry size",
      str(info.get("domains")))
check(info.get("auth_required") is True, "/ reports auth is required")
check(bool(info.get("endpoints")), "/ lists its endpoints")

r = CLIENT.get("/openapi.json")
check(r.status_code == 200 and "paths" in r.json(),
      "OpenAPI schema is generated (so /docs works)", str(r.status_code))
paths = r.json().get("paths", {})
for want in ("/solve", "/batch", "/domains", "/stats", "/health"):
    check(want in paths, f"OpenAPI documents {want}")


# ══════════════════════════════════════════════════════════════════════════
print("\n— authentication —")
# ══════════════════════════════════════════════════════════════════════════

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"})
check(r.status_code == 401, "no key -> 401", str(r.status_code))
check(r.json()["error"]["code"] == "missing_key", "no key -> missing_key",
      r.text[:160])

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"X-API-Key": "shk.garbage.nonsense"})
check(r.status_code == 401 and r.json()["error"]["code"] == "bad_signature",
      "forged key -> bad_signature", r.text[:160])

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"X-API-Key": A._encode(
                    A.KeyInfo(id="k_x", name="x", tier="vip", exp=0,
                              rpd=0, rpm=10, batch=5),
                    secret=secrets.token_urlsafe(48))})
check(r.status_code == 401 and r.json()["error"]["code"] == "bad_signature",
      "key signed with a different secret -> rejected", r.text[:160])

stale = A._encode(A.KeyInfo(id="k_old", name="old", tier="vip",
                            exp=int(time.time()) - 10, rpd=0, rpm=10, batch=5),
                  secret=SECRET)
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"X-API-Key": stale})
check(r.status_code == 403 and r.json()["error"]["code"] == "expired",
      "expired key -> 403 expired", r.text[:160])

revoked = key(name="doomed", tier="vip", days=30, key_id="k_revoked_me")
object.__setattr__(A.CFG, "revoked_ids", ("k_revoked_me",))
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"X-API-Key": revoked})
check(r.status_code == 403 and r.json()["error"]["code"] == "revoked",
      "revoked id -> 403 revoked (AUTH_REVOKED_IDS works)", r.text[:160])
object.__setattr__(A.CFG, "revoked_ids", ())

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"Authorization": f"Bearer {VIP}"})
check(r.status_code == 200, "Authorization: Bearer is accepted",
      str(r.status_code))

r = CLIENT.get(f"/solve?url={BASE}/go/hop1&key={VIP}")
check(r.status_code == 200, "?key= in the query string is accepted "
                            "(browser-testable)", str(r.status_code))

r = CLIENT.get("/domains")
check(r.status_code == 401, "/domains is protected too", str(r.status_code))


# ══════════════════════════════════════════════════════════════════════════
print("\n— resolving, end to end over HTTP —")
# ══════════════════════════════════════════════════════════════════════════

A.CACHE.clear()
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"}, headers=HDR)
check(r.status_code == 200, "POST /solve -> 200", f"{r.status_code} {r.text[:200]}")
d = r.json()
check(d["ok"] is True, "a 2-hop redirect chain resolves", json.dumps(d)[:300])
check(d["destination"] == DEST, f"destination is the real target ({DEST})",
      d.get("destination", ""))
check(d["hops"] >= 1, "hop count recorded", str(d.get("hops")))
check(d["cached"] is False, "first solve is not from cache")
check(d["elapsed_ms"] > 0, "elapsed_ms measured", str(d.get("elapsed_ms")))
check(d["error"] == "", "no error on success", d.get("error", ""))
check("shortener" in d and d["shortener"] == "127.0.0.1",
      "shortener host reported", str(d.get("shortener")))
check("chain" not in d, "hop trail omitted unless asked for")

r2 = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1", "chain": True},
                 headers=HDR)
d2 = r2.json()
check(d2["cached"] is True, "second identical solve is served from cache")
check(d2["elapsed_ms"] == 0, "a cache hit costs 0ms")
check(isinstance(d2.get("chain"), list) and d2["chain"],
      "chain=True returns the hop trail")
check(all(set(h) >= {"url", "kind"} for h in d2.get("chain", [])),
      "each hop has url and kind", json.dumps(d2.get("chain"))[:200])
check(A.CACHE.hits >= 1, "cache hit counter incremented")

r = CLIENT.post("/solve", json={"url": "not a url at all"}, headers=HDR)
check(r.status_code == 400 and r.json()["error"]["code"] == "bad_url",
      "junk input -> 400 bad_url, not a 500", r.text[:160])

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/dead"}, headers=HDR)
dd = r.json()
check(r.status_code == 200 and dd["ok"] is False,
      "an unresolvable link returns 200 with ok:false (not a 5xx)",
      r.text[:200])
check(bool(dd["error"]), "a failure always carries a reason", dd.get("error", ""))
check(dd["destination"] == "", "a failure returns no destination")

A.CACHE.clear()
check(A.CACHE.get(f"{BASE}/go/dead") is None, "failures are not cached")

# ── the bug a live test caught: a captcha reported as a successful resolve ──
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/captcha"}, headers=HDR)
dc = r.json()
check(r.status_code == 200 and dc["ok"] is False,
      "a captcha interstitial is NOT reported as a successful resolve",
      json.dumps(dc)[:300])
check(dc.get("challenge") == "DataDome",
      "the response names the anti-bot system that stopped it",
      str(dc.get("challenge")))
check(dc["destination"] == "",
      "no captcha URL is handed back as the destination",
      dc.get("destination", "")[:120])
check("anti-bot challenge" in dc["error"],
      "the error says what actually happened", dc.get("error", "")[:200])
check("proxy" in dc["error"].lower(),
      "the error names the fix (a residential proxy), not just the symptom")
check(not dc["cached"], "a challenge is never cached as a result")

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/captcha"}, headers=HDR)
check(r.json()["ok"] is False, "a challenge stays a failure on repeat calls "
                               "(the cache cannot resurrect it)")

# challenge_name(): precise on real challenge URLs, silent on innocent ones
for u, want in (("https://geo.captcha-delivery.com/captcha/?initialCid=AB", "DataDome"),
                ("https://challenges.cloudflare.com/turnstile/v0/api.js",
                 "Cloudflare Turnstile"),
                ("https://x.com/cdn-cgi/challenge-platform/h/b/orchestrate", "Cloudflare"),
                ("https://www.hcaptcha.com/captcha/", "hCaptcha"),
                ("https://api.geetest.com/gettype.php", "GeeTest"),
                ("https://site.com/_Incapsula_Resource?sw", "Imperva Incapsula")):
    check(A.challenge_name(url=u) == want,
          f"challenge_name recognises {want}", A.challenge_name(url=u))

for innocent in ("https://drive.google.com/file/d/1abc/view",
                 "https://example.com/how-to-solve-captcha-guide",
                 "https://mediafire.com/file/xyz/file.zip",
                 "https://youtube.com/watch?v=dQw4w9WgXcQ",
                 "https://github.com/user/repo/releases"):
    check(A.challenge_name(url=innocent) == "",
          f"no false positive on {innocent.split('/')[2]}",
          A.challenge_name(url=innocent))

check(A.challenge_name(text="<html>Just a moment...</html>") == "Cloudflare",
      "page-text detection works too")
check(A.challenge_name(text="<html>Download ready</html>") == "",
      "an ordinary page is not flagged by text")
check(not A.plausible_destination(
    "https://geo.captcha-delivery.com/captcha/?initialCid=A", "https://s.co/x"),
    "plausible_destination() refuses a captcha URL")
check(A.plausible_destination("https://mediafire.com/file/x/f.zip",
                             "https://s.co/x"),
      "plausible_destination() still accepts a real destination")

# ── the contract for `challenge`: it means "why this failed", nothing else ──
ok_wall = A.ResolveOutcome(input_url="http://x.co/a", ok=True,
                           final_url="https://real.example/final",
                           shortener="x.co", handler="auto", challenge="DataDome")
okd = A._serialize(ok_wall, True)
check("challenge" not in okd,
      "a success that hit a wall on a FURTHER hop does not advertise it",
      json.dumps(okd)[:260])
check(okd["ok"] is True and okd["destination"] == "https://real.example/final",
      "the genuine destination still comes back")
check(any("blocked by" in h["note"] for h in okd.get("chain", [])) is False,
      "and the chain is not fabricated for it")

fail_wall = A.ResolveOutcome(input_url="http://x.co/b", ok=False,
                             final_url="http://x.co/b", shortener="x.co",
                             handler="auto", challenge="Cloudflare",
                             error=A.challenge_error("Cloudflare"))
fd = A._serialize(fail_wall, False)
check(fd.get("challenge") == "Cloudflare",
      "a failure DOES name the anti-bot system", json.dumps(fd)[:260])
check("proxy" in fd["error"].lower(), "and the error names the fix")

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/meta"}, headers=HDR)
dm = r.json()
check(dm["ok"] is True and dm["destination"] == DEST,
      "a <meta http-equiv=refresh> interstitial resolves", json.dumps(dm)[:300])

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/selfloop"}, headers=HDR)
check(r.status_code == 200 and r.json()["ok"] is False,
      "an infinite redirect loop terminates and fails cleanly "
      "(MAX_HOPS, no hang)", r.text[:200])

r = CLIENT.post("/solve", json={"url": "gplinks.co/BFfOX"}, headers=HDR)
check(r.status_code == 200, "a bare host with no scheme is accepted",
      str(r.status_code))
check(A.normalize_url("gplinks.co/BFfOX") == "https://gplinks.co/BFfOX",
      "normalize_url still adds the scheme")

t0 = time.time()
r = CLIENT.get(f"/solve?url={BASE}/go/hop2", headers=HDR)
check(time.time() - t0 < A.CFG.solve_timeout + 5,
      "a single solve stays inside SOLVE_TIMEOUT")

r = CLIENT.get("/stats", headers=HDR)
st = r.json()
check(r.status_code == 200 and st["ok"] is True, "GET /stats works")
check(st["requests"] > 0 and st["solved"] > 0, "stats counted the solves",
      json.dumps({k: st[k] for k in ("requests", "solved", "failed")}))
check("p50" in st["latency_ms"], "latency percentiles reported")
check(st["your_key"]["tier"] == "vip", "stats echo the caller's tier")
check(any(x["domain"] == "127.0.0.1" for x in st["by_domain"]),
      "per-domain breakdown recorded", json.dumps(st["by_domain"])[:200])


# ══════════════════════════════════════════════════════════════════════════
print("\n— batch —")
# ══════════════════════════════════════════════════════════════════════════

A.CACHE.clear()
urls = [f"{BASE}/go/hop1", f"{BASE}/go/dead", f"{BASE}/go/meta"]
r = CLIENT.post("/batch", json={"urls": urls}, headers=HDR)
b = r.json()
check(r.status_code == 200 and b["ok"] is True, "POST /batch -> 200", r.text[:200])
check(b["count"] == 3, "all three attempted", str(b.get("count")))
check(b["solved"] == 2, "two solved, one failed", json.dumps(b)[:400])
check(b["failed"] == 1, "failure counted separately")
check(len(b["results"]) == 3 and b["results"][0]["destination"] == DEST,
      "results are in request order", json.dumps(b["results"][0])[:200])

r = CLIENT.post("/batch", json={"urls": urls, "parallel": True}, headers=HDR)
bp = r.json()
check(r.status_code == 200 and bp["count"] == 3,
      "parallel=True also works (opt-in)", r.text[:200])
check({x["destination"] for x in bp["results"] if x["ok"]} == {DEST},
      "parallel mode reaches the same destination")

small = key(name="small-batch", tier="free", days=1, batch=2)
r = CLIENT.post("/batch", json={"urls": urls}, headers={"X-API-Key": small})
check(r.status_code == 400
      and r.json()["error"]["code"] == "batch_too_large",
      "a key's batch limit is enforced", r.text[:200])
check(r.json().get("limit") == 2, "the error says what the limit is")

r = CLIENT.post("/batch", json={"urls": []}, headers=HDR)
check(r.status_code == 422 and r.json()["ok"] is False
      and r.json()["error"]["code"] == "validation_error",
      "an empty batch -> 422 in the same envelope as every other error",
      r.text[:220])
check(isinstance(r.json().get("details"), list) and r.json()["details"],
      "a 422 says which field failed", r.text[:220])
check("detail" not in r.json(),
      "no raw FastAPI 'detail' key leaks through", r.text[:220])

r = CLIENT.post("/solve", json={"nope": 1}, headers=HDR)
check(r.status_code == 422 and r.json()["error"]["code"] == "validation_error",
      "a missing 'url' field -> 422 validation_error", r.text[:220])

r = CLIENT.post("/batch", json={"urls": [""] * 3}, headers=HDR)
check(r.status_code == 400, "blank urls are rejected", r.text[:160])

r = CLIENT.post("/batch", json={"urls": ["x"] * 5000}, headers=HDR)
check(r.status_code in (400, 422), "an absurd batch is refused, not attempted",
      str(r.status_code))


# ══════════════════════════════════════════════════════════════════════════
print("\n— per-key rate limits and quotas —")
# ══════════════════════════════════════════════════════════════════════════

A.LIMITER.reset()
tight = key(name="tight", tier="free", days=1, rpm=2, rpd=0)
codes = [CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                     headers={"X-API-Key": tight}).status_code
         for _ in range(6)]
check(codes.count(429) > 0, f"rpm limit produces a 429 (got {codes})", str(codes))
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"X-API-Key": tight})
if r.status_code == 429:
    check("Retry-After" in r.headers, "429 carries Retry-After",
          str(dict(r.headers)))
    check(r.json()["error"]["code"] == "rate_limited", "429 says rate_limited")

A.LIMITER.reset()
daily = key(name="daily", tier="free", days=1, rpm=100000, rpd=2)
got = [CLIENT.post("/solve", json={"url": f"{BASE}/go/hop2"},
                   headers={"X-API-Key": daily}).status_code for _ in range(4)]
check(got[:2].count(429) == 0, "first calls within the daily quota pass",
      str(got))
check(429 in got[2:], f"daily quota produces a 429 (got {got})", str(got))
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop2"},
                headers={"X-API-Key": daily})
check(r.status_code == 429
      and r.json()["error"]["code"] == "quota_exhausted",
      "quota exhaustion is distinguishable from rate limiting", r.text[:200])

A.LIMITER.reset()
other_key = key(name="unaffected", tier="vip", days=1, rpm=100000)
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop2"},
                headers={"X-API-Key": other_key})
check(r.status_code == 200, "one key hitting its limit does not block another",
      str(r.status_code))


# ══════════════════════════════════════════════════════════════════════════
print("\n— SSRF guard (the reason this endpoint is not an open relay) —")
# ══════════════════════════════════════════════════════════════════════════

object.__setattr__(A.CFG, "allow_private_nets", False)
for target, why in (("http://169.254.169.254/latest/meta-data/iam/",
                     "cloud metadata"),
                    ("http://127.0.0.1:22/", "loopback"),
                    ("http://10.1.2.3/admin", "RFC1918"),
                    ("http://192.168.0.1/", "LAN gateway")):
    r = CLIENT.post("/solve", json={"url": target}, headers=HDR)
    body = r.text
    blocked = ("Blocked for safety" in body or "private range" in body
               or "non-public range" in body)
    check(blocked, f"refuses {why} ({target})", body[:200])
object.__setattr__(A.CFG, "allow_private_nets", True)

r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop2"}, headers=HDR)
check(r.status_code == 200, "re-enabling private nets restores the fixtures")


# ══════════════════════════════════════════════════════════════════════════
print("\n— admin surface —")
# ══════════════════════════════════════════════════════════════════════════

r = CLIENT.post("/admin/keys", params={"name": "sneaky", "tier": "vip"})
check(r.status_code == 403, "/admin/keys without the secret -> 403",
      str(r.status_code))
r = CLIENT.post("/admin/keys", params={"name": "sneaky", "tier": "vip"},
                headers={"X-Admin-Secret": "wrong"})
check(r.status_code == 403, "/admin/keys with a wrong secret -> 403")

r = CLIENT.post("/admin/keys", params={"name": "made-over-http", "tier": "vip",
                                       "days": 7},
                headers={"X-Admin-Secret": ADMIN})
mk = r.json()
check(r.status_code == 200 and mk["ok"] is True, "admin can mint over HTTP",
      r.text[:200])
check(mk["info"]["name"] == "made-over-http", "minted key carries the name")
check(A.verify_key(mk["key"], secret=SECRET).tier == "vip",
      "a key minted over HTTP actually verifies")

r = CLIENT.get("/admin/keys/verify", params={"key": VIP},
               headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 200 and r.json()["info"]["tier"] == "vip",
      "admin can inspect a key")
r = CLIENT.get("/admin/keys/verify", params={"key": "shk.bad.sig"},
               headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 401, "verifying junk returns the key error, not a 500",
      str(r.status_code))

r = CLIENT.put("/admin/delays/gplinks.co", params={"seconds": 12},
               headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 200 and abs(A.DELAYS.get("gplinks.co") - 12) < 0.01,
      "admin can pin a domain's anti-bot delay", r.text[:200])
r = CLIENT.get("/admin/delays", headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 200 and r.json()["learned"].get("gplinks.co") == 12.0,
      "pinned delay is visible")
r = CLIENT.put("/admin/delays/gplinks.co", params={"seconds": 99999},
               headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 422, "an absurd delay is refused by validation",
      str(r.status_code))

r = CLIENT.delete("/admin/cache", headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 200 and len(A.CACHE._d) == 0,
      "admin can clear the cache", r.text[:160])

object.__setattr__(A.CFG, "admin_secret", "")
r = CLIENT.post("/admin/keys", params={"name": "x"},
                headers={"X-Admin-Secret": ADMIN})
check(r.status_code == 503 and r.json()["error"]["code"] == "admin_disabled",
      "with no ADMIN_SECRET the whole /admin surface is disabled, not open",
      r.text[:200])
object.__setattr__(A.CFG, "admin_secret", ADMIN)


# ══════════════════════════════════════════════════════════════════════════
print("\n— deployment modes —")
# ══════════════════════════════════════════════════════════════════════════

object.__setattr__(A.CFG, "auth_required", False)
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"})
check(r.status_code == 200 and r.json()["ok"] is True,
      "AUTH_REQUIRED=false opens the endpoint (local testing)", r.text[:200])
object.__setattr__(A.CFG, "auth_required", True)
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"})
check(r.status_code == 401, "and closing it again works")

static = "static-shared-secret-for-test"
object.__setattr__(A.CFG, "static_api_key", static)
r = CLIENT.post("/solve", json={"url": f"{BASE}/go/hop1"},
                headers={"X-API-Key": static})
check(r.status_code == 200, "a shared static API_KEY works alongside signed keys",
      str(r.status_code))
info = A.verify_key(static)
check(info.tier == "vip" and info.id == "static",
      "the static key is treated as VIP")
object.__setattr__(A.CFG, "static_api_key", "")

check(A.CFG.host == "0.0.0.0",
      f"HOST defaults to 0.0.0.0, which Render requires (got {A.CFG.host})")
check(A.CFG.port == int(os.environ.get("PORT", "8080")),
      "PORT is read from the environment (Render injects it)")
probs = A.preflight()
check(len(probs) == 1 and "ALLOW_PRIVATE_NETWORKS" in probs[0],
      "preflight flags the open SSRF relay this test process enables",
      str(probs)[:300])

object.__setattr__(A.CFG, "auth_secret", "")
object.__setattr__(A.CFG, "static_api_key", "")
probs = A.preflight()
check(any("AUTH_SECRET" in p for p in probs),
      "preflight refuses to start with no AUTH_SECRET", str(probs)[:200])
object.__setattr__(A.CFG, "auth_secret", "short")
check(any("at least 32" in p for p in A.preflight()),
      "preflight rejects a weak AUTH_SECRET")
object.__setattr__(A.CFG, "auth_secret", SECRET)

object.__setattr__(A.CFG, "host", "127.0.0.1")
check(any("0.0.0.0" in p for p in A.preflight()),
      "preflight warns when HOST would make Render unreachable")
object.__setattr__(A.CFG, "host", "0.0.0.0")
probs = A.preflight()
check(len(probs) == 1 and "ALLOW_PRIVATE_NETWORKS" in probs[0],
      "restoring HOST leaves only the SSRF-relay warning (which this test "
      "process enables on purpose)", str(probs)[:300])

r = CLIENT.get("/docs")
check(r.status_code == 200, "interactive docs are served at /docs",
      str(r.status_code))


# ══════════════════════════════════════════════════════════════════════════
print("\n— CLI —")
# ══════════════════════════════════════════════════════════════════════════

check(len(A._b64e(b"abc")) == 4 and "=" not in A._b64e(b"abc"),
      "base64url output is unpadded (URL-safe)")
check(A._b64d(A._b64e(b"round trip")) == b"round trip", "b64 round-trips")

k, i = A.mint_key("cli", "free", 5, secret=SECRET)
check(i.tier == "free" and i.batch <= A.CFG.batch_limit,
      "free keys get a smaller batch limit than VIP")
kv, iv = A.mint_key("cli", "vip", 5, secret=SECRET)
check(iv.batch > i.batch, f"vip batch ({iv.batch}) > free batch ({i.batch})")
check(iv.rpm > i.rpm, f"vip rpm ({iv.rpm}) > free rpm ({i.rpm})")
check(iv.rpd == 0, "vip has no daily cap by default")
check(i.rpd == A.CFG.free_daily_limit, "free gets FREE_DAILY_LIMIT per day")
check(A.mint_key("x", "vip", 5, key_id="fixed", secret=SECRET)[1].id == "fixed",
      "an explicit key id is honoured (needed for AUTH_REVOKED_IDS)")

import contextlib, io as _io
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = A.main(["--print-secret"])
printed = buf.getvalue().strip()
check(rc == 0 and len(printed) >= 32, "--print-secret emits a usable secret")

# With the SSRF guard on (the production shape) preflight must be clean and
# --selfcheck must exit 0. The self-check forces the guard on internally, so
# it verifies the block logic even when the operator disabled it.
object.__setattr__(A.CFG, "allow_private_nets", False)
check(A.preflight() == [], f"preflight clean for a production config: {A.preflight()}")
buf = _io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = A.main(["--selfcheck"])
check(rc == 0, "--selfcheck exits 0 on a production config",
      buf.getvalue()[-400:])
check("SSRF guard refuses 169.254.169.254" in buf.getvalue(),
      "--selfcheck really did exercise the SSRF guard")
object.__setattr__(A.CFG, "allow_private_nets", True)


SRV.shutdown()
print("\n" + "=" * 70)
print(f"RESULT: {PASS} passed, {FAIL} failed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  - {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
