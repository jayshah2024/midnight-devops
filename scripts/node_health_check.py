#!/usr/bin/env python3
"""
node_health_check.py — Midnight pre-prod node health checker (Take-home Section 3, Option C)

Polls a Midnight node's JSON-RPC endpoint, evaluates a set of health conditions, writes a
structured JSON health report to disk, and diffs against the previous report to surface
regressions (e.g. peers dropped, height stopped advancing, a check that flipped pass -> fail).

Design goals:
  - Re-runnable & idempotent: each run writes a timestamped report plus updates `latest.json`.
    Running it twice does not corrupt state; the previous `latest.json` is the diff baseline.
  - Dependency-free: standard library only (urllib/json), so it runs anywhere Python 3.8+ is.
  - Operationally honest exit codes: 0 = healthy, 1 = degraded/critical, 2 = could not reach
    node. Drops cleanly into cron, a systemd timer, or a CI gate.

Usage:
    # one-shot check
    ./node_health_check.py --rpc-url http://localhost:9944 --report-dir ./health

    # run continuously every 30s (e.g. under tmux / a service)
    ./node_health_check.py --rpc-url http://localhost:9944 --interval 30 --report-dir ./health

    # tune thresholds
    ./node_health_check.py --min-peers 3 --stall-secs 180 --max-disk-pct 85

Exit codes:
    0  all checks healthy
    1  one or more checks degraded/critical (node reachable but unhealthy)
    2  node unreachable / RPC error
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Status levels (ordered: worst wins when we aggregate)
# ---------------------------------------------------------------------------
OK, WARN, CRIT, UNKNOWN = "ok", "warn", "crit", "unknown"
_SEVERITY = {OK: 0, UNKNOWN: 1, WARN: 2, CRIT: 3}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# RPC client
# ---------------------------------------------------------------------------
def rpc_call(url: str, method: str, params: Optional[list] = None,
             timeout: float = 10.0) -> Any:
    """Single JSON-RPC call. Raises on transport/RPC error so callers can mark UNKNOWN."""
    payload = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method,
                          "params": params or []}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    if "error" in body and body["error"]:
        raise RuntimeError(f"RPC error for {method}: {body['error']}")
    return body.get("result")


def _to_int(value: Any) -> Optional[int]:
    """Substrate RPC returns numbers as hex strings (e.g. '0x1a4'). Be tolerant."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Individual health checks. Each returns a dict the report can serialize directly.
# ---------------------------------------------------------------------------
def check_reachable(url: str) -> dict:
    try:
        health = rpc_call(url, "system_health")
        return {"status": OK, "detail": "RPC reachable", "raw": health}
    except (urllib.error.URLError, socket.timeout, RuntimeError, ValueError) as e:
        return {"status": CRIT, "detail": f"RPC unreachable: {e}", "raw": None}


def check_peers(health: Optional[dict], min_peers: int) -> dict:
    if not health or "peers" not in health:
        return {"status": UNKNOWN, "detail": "no peer data", "peers": None}
    peers = health.get("peers")
    status = OK if peers >= min_peers else WARN
    return {"status": status, "detail": f"{peers} peers (min {min_peers})", "peers": peers}


def check_syncing(health: Optional[dict]) -> dict:
    if not health or "isSyncing" not in health:
        return {"status": UNKNOWN, "detail": "no sync data", "isSyncing": None}
    syncing = health.get("isSyncing")
    # Still syncing isn't an outage, but it's worth surfacing as WARN until caught up.
    return {"status": WARN if syncing else OK,
            "detail": "catching up" if syncing else "synced to tip",
            "isSyncing": syncing}


def check_block_height(url: str, prev_report: Optional[dict], stall_secs: int) -> dict:
    """Height check + stall detection against the previous report's height/timestamp."""
    try:
        header = rpc_call(url, "chain_getHeader")
        height = _to_int(header.get("number")) if header else None
    except (urllib.error.URLError, socket.timeout, RuntimeError, ValueError) as e:
        return {"status": UNKNOWN, "detail": f"height query failed: {e}", "height": None}

    if height is None:
        return {"status": UNKNOWN, "detail": "no height returned", "height": None}

    result = {"height": height}
    prev = (prev_report or {}).get("checks", {}).get("block_height", {})
    prev_height = prev.get("height")
    prev_ts = (prev_report or {}).get("timestamp")

    if prev_height is None or prev_ts is None:
        result.update(status=OK, detail=f"height {height} (no baseline yet)")
        return result

    if height > prev_height:
        result.update(status=OK, detail=f"height advancing: {prev_height} -> {height}")
        return result

    # Height did not advance — how long has it been stuck?
    try:
        elapsed = (datetime.strptime(now_iso(), "%Y-%m-%dT%H:%M:%SZ")
                   - datetime.strptime(prev_ts, "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
    except ValueError:
        elapsed = 0
    status = CRIT if elapsed >= stall_secs else WARN
    result.update(status=status,
                  detail=f"height STALLED at {height} for ~{int(elapsed)}s "
                         f"(threshold {stall_secs}s)")
    return result


def check_disk(path: str, max_pct: float) -> dict:
    try:
        usage = shutil.disk_usage(path)
        pct = usage.used / usage.total * 100
    except OSError as e:
        return {"status": UNKNOWN, "detail": f"disk stat failed: {e}", "used_pct": None}
    status = WARN if pct >= max_pct else OK
    return {"status": status,
            "detail": f"disk {pct:.1f}% used (threshold {max_pct}%)",
            "used_pct": round(pct, 1)}


# ---------------------------------------------------------------------------
# Report assembly, persistence, and diffing
# ---------------------------------------------------------------------------
def aggregate(checks: dict) -> str:
    worst = OK
    for c in checks.values():
        if _SEVERITY[c["status"]] > _SEVERITY[worst]:
            worst = c["status"]
    return worst


def load_latest(report_dir: Path) -> Optional[dict]:
    latest = report_dir / "latest.json"
    if latest.exists():
        try:
            return json.loads(latest.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def diff_reports(prev: Optional[dict], curr: dict) -> list[str]:
    """Surface regressions: checks whose severity worsened, or notable metric drops."""
    if not prev:
        return ["no previous report — this is the baseline"]
    regressions: list[str] = []
    prev_checks = prev.get("checks", {})
    for name, c in curr["checks"].items():
        p = prev_checks.get(name)
        if not p:
            continue
        if _SEVERITY[c["status"]] > _SEVERITY[p["status"]]:
            regressions.append(f"{name}: {p['status']} -> {c['status']} ({c['detail']})")
        # Explicit peer-drop callout even within the same status band.
        if name == "peers" and p.get("peers") is not None and c.get("peers") is not None:
            if c["peers"] < p["peers"]:
                regressions.append(f"peers dropped: {p['peers']} -> {c['peers']}")
    return regressions or ["no regressions vs previous report"]


def write_report(report_dir: Path, report: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["timestamp"].replace(":", "").replace("-", "")
    path = report_dir / f"health_{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    # Atomic-ish update of the diff baseline.
    tmp = report_dir / "latest.json.tmp"
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, report_dir / "latest.json")
    return path


def run_once(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir)
    prev = load_latest(report_dir)

    reach = check_reachable(args.rpc_url)
    health = reach.get("raw")

    checks = {
        "reachable": reach,
        "peers": check_peers(health, args.min_peers),
        "syncing": check_syncing(health),
        "block_height": check_block_height(args.rpc_url, prev, args.stall_secs),
        "disk": check_disk(args.disk_path, args.max_disk_pct),
    }

    overall = aggregate(checks)
    report = {
        "timestamp": now_iso(),
        "rpc_url": args.rpc_url,
        "overall": overall,
        "checks": checks,
    }
    report["regressions"] = diff_reports(prev, report)

    path = write_report(report_dir, report)

    # Human-readable line for logs/cron mail; full detail is in the JSON.
    print(f"[{report['timestamp']}] overall={overall.upper()}  -> {path.name}")
    for name, c in checks.items():
        print(f"  {name:13s} {c['status']:7s} {c['detail']}")
    for r in report["regressions"]:
        print(f"  diff: {r}")

    if not reach["status"] == OK:
        return 2
    return 0 if overall in (OK,) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Midnight node health checker (Option C)")
    p.add_argument("--rpc-url", default="http://localhost:9944",
                   help="Node JSON-RPC endpoint (default: %(default)s)")
    p.add_argument("--report-dir", default="./health",
                   help="Directory for JSON reports + latest.json baseline")
    p.add_argument("--interval", type=int, default=0,
                   help="Seconds between checks; 0 = run once and exit (default: 0)")
    p.add_argument("--min-peers", type=int, default=3,
                   help="Warn if connected peers below this (default: %(default)s)")
    p.add_argument("--stall-secs", type=int, default=180,
                   help="Mark height CRIT if not advancing for this long (default: %(default)s)")
    p.add_argument("--max-disk-pct", type=float, default=85.0,
                   help="Warn if disk usage exceeds this %% (default: %(default)s)")
    p.add_argument("--disk-path", default="/",
                   help="Path to check for disk usage (default: %(default)s)")
    args = p.parse_args()

    if args.interval <= 0:
        return run_once(args)

    # Continuous mode. Worst exit code is informational here; loop until interrupted.
    last_rc = 0
    try:
        while True:
            last_rc = run_once(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return last_rc


if __name__ == "__main__":
    sys.exit(main())
