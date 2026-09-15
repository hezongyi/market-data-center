#!/usr/bin/env python3
"""Exercise two managed previews through their public HTTP and lifecycle boundary."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def command(python: Path, base: Path, action: str, preview_id: str, *extra: str) -> dict:
    env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("DATACENTER_", "DUKASCOPY_", "FRED_", "BINANCE_", "YFINANCE_"))
    }
    result = subprocess.run(
        [str(python), str(REPO / "scripts/dev_preview.py"), action, "--id", preview_id,
         "--base", str(base), "--python", str(python), "--json", *extra],
        cwd=REPO, env=env, capture_output=True, text=True, check=False, timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"preview {action} failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def request(url: str, *, method: str = "GET", payload: dict | None = None,
            origin: str | None = None, opener=None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if origin:
        headers["Origin"] = origin
    operation = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with (opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))).open(operation, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mdc-preview-acceptance-") as directory:
        base = Path(directory)
        running: set[str] = set()
        try:
            alpha = command(args.python, base, "start", "alpha")
            running.add("alpha")
            beta = command(args.python, base, "start", "beta")
            running.add("beta")
            assert alpha["state"] == beta["state"] == "running"
            assert alpha["scheduler"]["effective_dispatch"] is True
            assert beta["scheduler"]["effective_dispatch"] is True
            assert alpha["ui_url"] != beta["ui_url"]
            assert alpha["data_root"] != beta["data_root"]

            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
            status, initialized = request(
                alpha["ui_url"] + "/api/v1/auth/initialize", method="POST",
                payload={"username": "admin", "password": "preview-acceptance-password"},
                origin=alpha["ui_url"], opener=opener,
            )
            assert status == 200 and initialized["data"]["initialized"] is True
            status, _ = request(
                alpha["ui_url"] + "/api/v1/auth/login", method="POST",
                payload={"username": "admin", "password": "preview-acceptance-password"},
                origin=alpha["ui_url"], opener=opener,
            )
            assert status == 200
            assert request(alpha["ui_url"] + "/api/v1/auth/me", opener=opener)[0] == 200
            assert request(beta["ui_url"] + "/api/v1/auth/me", opener=opener)[0] == 401

            command(args.python, base, "stop", "alpha")
            running.remove("alpha")
            assert command(args.python, base, "status", "beta")["state"] == "running"
            alpha = command(args.python, base, "start", "alpha")
            running.add("alpha")
            assert request(alpha["ui_url"] + "/api/v1/auth/status")[1]["data"]["initialized"] is True
        finally:
            for preview_id in tuple(running):
                command(args.python, base, "stop", preview_id)
        print(json.dumps({
            "status": "pass",
            "checks": ["four_process_start", "parallel_isolation", "same_origin_auth",
                       "cookie_isolation", "independent_stop", "restart_retention"],
        }))


if __name__ == "__main__":
    main()
