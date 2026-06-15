# scripts/ — Automation (Section 3, Option C)

## node_health_check.py — node health checker

Polls the Midnight node's JSON-RPC endpoint, evaluates health conditions, writes a structured
JSON report to disk, and **diffs against the previous run** to surface regressions. Standard
library only (Python 3.8+) — no dependencies to install.

### Usage

```bash
# One-shot check (writes a report under ./health and updates latest.json)
./node_health_check.py --rpc-url http://localhost:9944 --report-dir ./health

# Continuous, every 30s (e.g. under a systemd timer or tmux)
./node_health_check.py --rpc-url http://localhost:9944 --interval 30 --report-dir ./health

# Tune thresholds
./node_health_check.py --min-peers 3 --stall-secs 180 --max-disk-pct 85
```

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--rpc-url` | `http://localhost:9944` | Node JSON-RPC endpoint |
| `--report-dir` | `./health` | Where reports + `latest.json` baseline are written |
| `--interval` | `0` | Seconds between checks; `0` = run once and exit |
| `--min-peers` | `3` | Warn below this peer count |
| `--stall-secs` | `180` | Mark height CRIT if not advancing for this long |
| `--max-disk-pct` | `85` | Warn above this disk usage |
| `--disk-path` | `/` | Filesystem to check |

### Exit codes (drops cleanly into cron / CI)

- `0` — all checks healthy
- `1` — node reachable but one or more checks degraded/critical
- `2` — node unreachable / RPC error
