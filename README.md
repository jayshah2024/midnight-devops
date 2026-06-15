# Midnight Network — DevOps Take-Home

This repo is my submission for the Midnight DevOps assessment. It stands up a Midnight
pre-prod full node on GCP, monitors it with the Google Cloud Ops Agent + Datadog, and ships a
health-check automation script. Below is a summary of what's here, the design decisions,
assumptions, and what I'd do with more time.
 
## Section 1 — Node setup (RUNBOOK.md)

A GCP VM (Ubuntu 22.04, e2-standard-4, SSD) running the Midnight pre-prod stack from
**native binaries** managed as systemd services: `cardano-node` → `cardano-db-sync` →
PostgreSQL, with `midnight-node` on top. I synced Cardano DB Sync first (the ~6h hard
prerequisite), then brought up the Midnight node as a **full node with the CLI/RPC attached**.
(The stack also runs locally on Apple Silicon via Multipass using the arm64 binaries — see
RUNBOOK — but a GCP SSD VM syncs far faster.)

**Outcome / honest status:** I got the node onto the correct pre-prod chain (peers advertising
the real ~1.15M tip), with db-sync fully synced and the Cardano data-source configured, but the
node is blocked importing historical blocks: it rejects a main-chain reference as a *"stable
block outside allowed range"* across an apparent ~8h Cardano main-chain gap around 2026-03-20.
I traced this through every layer (env config → chain-spec/bootnodes → db-sync sync → the
stability-window math) and narrowed it to a **node-version / network-compatibility** issue, not
a setup error — the full sequential debugging is in **RUNBOOK.md Appendix**. Per the brief,
a documented genuine blocker is a valid response; resolution path (match the pre-prod node
version per Midnight's compatibility matrix) is noted there.


## Section 2 — Monitoring & alerting (Cloud Ops Agent → Cloud Monitoring/GMP → Datadog → Squadcast/Slack)

A single **Google Cloud Ops Agent** on the VM does everything: collects host metrics
(→ Cloud Monitoring, `agent.googleapis.com/*`), scrapes the node's Prometheus endpoints
(`midnight-node` :9615, `cardano-node` :12798) via its built-in prometheus receiver
(→ Managed Service for Prometheus, `prometheus.googleapis.com/*`), and ships service logs
(→ Cloud Logging) — all authed by the VM's service account. **Datadog's Google Cloud
integration** surfaces those metrics, where **custom Datadog monitor queries** evaluate them
and route by severity to **Squadcast** (paging) and **Slack** (warnings). Config is in
`monitoring/` (see `monitoring/README.md`).

Why this shape (vs Prometheus + Grafana): one Google-managed agent replaces a self-hosted
Prometheus server, node_exporter, and Grafana — no TSDB to operate, no exporter to run, no
dashboard server to secure, and metrics **and** logs come from the same agent with keyless
auth. Metrics land in managed Cloud Monitoring/GMP, and Datadog's existing GCP integration
pulls them in for dashboards **and** alerting (which Grafana alone doesn't do), giving on-call
one pane of glass alongside the rest of the GCP infra. The off-VM-alerting risk — the
agent→GMP→Datadog path breaking — is covered by the node-down / **no-data** monitor, which
pages on absence of metrics. Tradeoff: GCP lock-in and dual metering (GMP samples + Datadog
custom metrics) vs the lower operational surface; for a multi-cloud fleet, Prometheus + Grafana
becomes more attractive again.

### The three alerts and why

The alerts are chosen for **signal over noise** — each maps to a distinct, actionable
failure mode, and each has a clear operational response. A validator's whole job is to stay
in sync and reachable, so the alerts watch exactly those properties plus the resource that
most commonly kills it.

1. **Block height stalled** — alert if the node's reported block height does not increase
   for N minutes (derivative ≈ 0 on the height metric).
   *Why:* a node that's "Up" but not advancing is silently useless — this is the single
   most important validator health signal, and process-level "is it running" checks miss it.
   *Response:* check peer count and db-sync lag first (most stalls trace back to lost peers
   or main-chain lag), inspect logs, restart the node if wedged.

2. **Peer count below threshold** — alert if connected peers drop below a floor (e.g. < 3)
   for a sustained window.
   *Why:* peers trend toward zero *before* height stalls, so this is the leading indicator
   that buys you time to act before you actually fall out of sync.
   *Response:* verify outbound networking / firewall, confirm bootnodes/topology config,
   check for a chainspec/version mismatch.

3. **Host resource saturation (CPU/mem/disk) + node-down** — alert if CPU or memory
   crosses a sustained threshold, **disk usage > 85%** (from the Ops Agent host metrics,
   `gcp.agent.*`), or the node stops reporting (`up == 0` from the scraped prometheus target,
   plus a **no-data** condition that fires if metrics stop arriving at all).
   *Why:* on this stack the most common real-world outage is **disk filling from db-sync**,
   which wedges Postgres and the node together. Disk at 85% is an early warning with time to
   intervene before a hard failure. The node-down / no-data check catches crashes and a broken
   telemetry pipeline directly — important because alert evaluation now lives off-VM in Datadog.
   *Response:* for disk, grow the volume or prune; for node-down, inspect logs for the cause
   before restarting so you don't just mask a recurring fault.

Thresholds use sustained windows (not single-sample spikes) to avoid flapping. Severities
are split so "height stalled" / "process down" page, while "peers low" / "disk 85%" warn.

## Section 3 — Automation (Option C: node health checker)

`scripts/` contains a **node health checker** that polls the node's RPC/metrics on a
configurable interval, evaluates a set of health conditions (height advancing, peer count,
sync state, resource thresholds), and writes a **structured JSON health report** to disk.
It keeps the previous report and **diffs** against it to surface regressions (e.g. peers
dropped, height stopped advancing, a check that flipped from pass→fail). It's idempotent
and re-runnable; exit code reflects health so it drops cleanly into cron or a CI gate.

Usage and flags are documented inline and in `scripts/README` / the header comment.


## What I'd do with more time

- **Infrastructure as code:** the VM + firewall + agent install are currently
  semi-manual; I'd codify them in Terraform and an Ansible/cloud-init bootstrap so the
  whole node is reproducible from one `apply`.
- **Synthetic block-production check** once whitelisted as a real FNO — alert on missed
  *slots*, not just height, which is the metric that actually matters for a validator's rewards/standing.
- **Dashboards as code:** export the Datadog dashboard JSON into `monitoring/` so the
  visual layer is version-controlled alongside the monitors.
- **Monitors as code:** manage the Datadog monitors + GCP integration via Terraform
  (`datadog_monitor`, `datadog_integration_gcp_sts`) instead of API POSTs.
- **Secrets:** move key material to a cloud KMS/secrets manager with the rotation flow
  described in `SECURITY.md`, rather than env files.
- **Alert runbooks:** link each alert to a short remediation doc so on-call has a one-click
  path from page to fix.
