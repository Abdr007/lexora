#!/usr/bin/env python3
"""Pre-flight the deployed stack, the way `make demo` pre-flights the local one.

`make demo` proves the eight states against a local API. Nothing proved them against the
deployment, so every hosted failure so far was found by opening the page and looking —
including a UI that redirected every visitor to a Vercel login, and a CSP that pinned
`connect-src` to localhost and blocked every call from inside the browser. Neither leaves
a trace in the API's logs, because in both cases no request ever reaches the API.

Run it before a demo. It also warms the Space, which sleeps after 48 hours idle and takes
roughly 30 seconds to wake — so the first question of the call is not the slow one.

    make verify-hosted

Standard library only, deliberately: this checks a deployment, and should not itself
depend on a working install.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Final

UI_DEFAULT: Final = "https://uselexora.vercel.app"
API_DEFAULT: Final = "https://Abdr007-lexora.hf.space"

# A cold Space wakes in ~30 s and then loads two ONNX sessions.
TIMEOUT_S: Final = 120
HTTP_OK: Final = 200
HTTP_FOUND: Final = 302


class CheckError(Exception):
    """A check that did not hold. The message is what the presenter needs to read."""


def _lower_keys(headers: Any) -> dict[str, str]:
    """Header names, lower-cased.

    ``dict(response.headers)`` keeps the wire casing and loses the case-insensitive
    lookup the underlying ``Message`` provides. Over HTTP/2 the names arrive lower-cased
    anyway, so a lower-cased lookup against that dict works — and then silently stops
    working over HTTP/1.1, where Vercel sends ``Content-Security-Policy``. This checker
    reported the CSP as absent for exactly that reason.
    """
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _request(
    url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            return int(response.status), _lower_keys(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), _lower_keys(exc.headers), exc.read()


def _json(url: str, *, body: dict[str, Any] | None = None, origin: str) -> Any:
    headers = {"Origin": origin}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    status, _, raw = _request(url, method="POST" if body else "GET", body=payload, headers=headers)
    if status != HTTP_OK:
        raise CheckError(f"{url} returned HTTP {status}")
    return json.loads(raw)


def check_ui_is_public(ui: str) -> str:
    """A 302 here means deployment protection is on and visitors hit a login page."""
    status, headers, _ = _request(ui, headers={})
    if status == HTTP_FOUND:
        location = headers.get("location", "")
        if "vercel.com" in location:
            raise CheckError(
                "the UI redirects to Vercel login — deployment protection is enabled. "
                "Project → Settings → Deployment Protection → disable Vercel Authentication"
            )
        raise CheckError(f"the UI redirects to {location}")
    if status != HTTP_OK:
        raise CheckError(f"the UI returned HTTP {status}")
    return f"HTTP 200 at {ui}"


def check_csp_points_at_the_api(ui: str, api: str) -> str:
    """The CSP is baked at build time; a stale one blocks every call inside the browser."""
    _, headers, _ = _request(ui, headers={})
    csp = headers.get("content-security-policy", "")
    if not csp:
        raise CheckError("the UI sends no Content-Security-Policy")
    connect = next((part for part in csp.split(";") if part.strip().startswith("connect-src")), "")
    host = api.split("://", 1)[-1]
    if host.lower() not in connect.lower():
        raise CheckError(
            f"CSP connect-src does not name {host} — it reads '{connect.strip()}'. "
            "NEXT_PUBLIC_API_URL was not set at BUILD time; redeploy after setting it"
        )
    return f"connect-src names {host}"


def check_api_health(api: str, origin: str) -> str:
    data = _json(f"{api}/api/health", origin=origin)
    if data.get("status") != "ok":
        raise CheckError(f"health is '{data.get('status')}': {data.get('detail')}")
    chunks, points = data.get("chunks_indexed", 0), data.get("vector_points", 0)
    if chunks == 0:
        raise CheckError("no chunks indexed — the image shipped without the index")
    if chunks != points:
        raise CheckError(
            f"{chunks} chunks but {points} vector points — the collection is incomplete"
        )
    return f"{chunks} chunks, {points} vectors, engine={data.get('engine')}"


def check_cors_allows_the_ui(api: str, ui: str) -> str:
    status, headers, _ = _request(
        f"{api}/api/ask",
        method="OPTIONS",
        headers={
            "Origin": ui,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    allowed = headers.get("access-control-allow-origin", "")
    if status != HTTP_OK or ui not in allowed:
        raise CheckError(
            f"pre-flight from {ui} returned HTTP {status} allow-origin='{allowed}'. "
            "Set LEXORA_CORS_ALLOW_ORIGINS on the Space to include the UI origin"
        )
    return f"pre-flight allows {ui}"


def check_refuses(api: str, origin: str, question: str, label: str) -> str:
    data = _json(f"{api}/api/ask", body={"question": question}, origin=origin)
    if data.get("kind") != "refusal":
        raise CheckError(f"{label}: expected a refusal, got '{data.get('kind')}'")
    return f"{label} → refusal"


def check_answers(api: str, origin: str, question: str) -> str:
    data = _json(f"{api}/api/ask", body={"question": question}, origin=origin)
    if data.get("kind") != "answer":
        raise CheckError(f"expected an answer, got '{data.get('kind')}'")
    citations = data.get("citations") or []
    if not citations:
        raise CheckError("answered with no citations — the whole point is the citation")
    articles = [c.get("article_no") for c in citations]
    return f"answer citing articles {articles}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui", default=UI_DEFAULT)
    parser.add_argument("--api", default=API_DEFAULT)
    args = parser.parse_args(argv)
    ui, api = args.ui.rstrip("/"), args.api.rstrip("/")

    checks: list[tuple[str, Any]] = [
        ("UI is public", lambda: check_ui_is_public(ui)),
        ("CSP points at the API", lambda: check_csp_points_at_the_api(ui, api)),
        ("API health", lambda: check_api_health(api, ui)),
        ("CORS allows the UI", lambda: check_cors_allows_the_ui(api, ui)),
        (
            "refusal · out of corpus",
            lambda: check_refuses(
                api, ui, "What is the capital gains tax rate in Singapore?", "Singapore"
            ),
        ),
        (
            "refusal · wrong jurisdiction",
            lambda: check_refuses(
                api,
                ui,
                "What is the notice period under Saudi Arabian labour law?",
                "Saudi Arabia",
            ),
        ),
        (
            "answers with citations",
            lambda: check_answers(api, ui, "How much end-of-service money do I get after 6 years?"),
        ),
    ]

    print(f"  UI   {ui}\n  API  {api}\n")
    failures: list[str] = []
    for name, run in checks:
        started = time.perf_counter()
        try:
            detail = run()
            elapsed = time.perf_counter() - started
            print(f"  PASS  {name:28} {detail}  [{elapsed:.1f}s]")
        except CheckError as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAIL  {name:28} {exc}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAIL  {name:28} unreachable: {exc}")

    if failures:
        print(f"\n  {len(failures)} of {len(checks)} checks failed — do not present\n")
        return 1
    print(f"\n  all {len(checks)} checks passed · the Space is warm · ready to present\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
