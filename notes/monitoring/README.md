# Monitoring — Cloud Ops Agent → Cloud Monitoring/GMP → Datadog → Squadcast/Slack (Section 2)

Telemetry for the pre-prod Midnight node on GCP. A single **Google Cloud Ops Agent** on the VM
collects host metrics, scrapes the node's Prometheus endpoints, and ships logs — all to Google
Cloud's managed backends. **Datadog's Google Cloud integration** surfaces the metrics, where
**custom Datadog monitor queries** evaluate them and route to **Squadcast** (paging) and
**Slack** (warnings).

## Services used (each box in the path)

| # | Service | Role here |
|---|---|---|
| 1 | **Cloud Ops Agent** (on the VM) | One Google-managed agent: collects host metrics, scrapes the two app Prometheus endpoints, and tails service logs. Auth = VM service account. |
| 2 | **Cloud Monitoring** | Metric store. Receives Ops Agent **host** metrics under `agent.googleapis.com/*`. |
| 3 | **Managed Service for Prometheus (GMP)** | The Prometheus-format ingestion layer the Ops Agent's prometheus receiver writes to; stores **app** metrics in Cloud Monitoring under `prometheus.googleapis.com/*`. |
| 4 | **Cloud Logging** | Log store for the three services' journald logs (correlation). |
| 5 | **Datadog** (Google Cloud integration) | Pulls the metrics from Cloud Monitoring → dashboards + alert evaluation. |
| 6 | **Squadcast** | On-call paging / incident management for critical alerts. |
| 7 | **Slack** | `#node-ops-alerts` for warnings. |

## Architecture

```
        ┌──────────────────── GCP VM ────────────────────┐
        │                Cloud Ops Agent                  │
        │   ┌───────────────┬──────────────────┬───────┐  │
        │   │ host metrics  │ prometheus recv  │ logs  │  │
        │   │ (cpu/mem/disk)│ :9615 + :12798   │ journald
        │   └──────┬────────┴────────┬─────────┴───┬───┘  │
        └──────────┼─────────────────┼─────────────┼──────┘
                   ▼                 ▼             ▼
        Cloud Monitoring         GMP            Cloud Logging
       agent.googleapis.com  prometheus.googleapis.com
                   └────────┬────────┘
                            ▼  (Datadog GCP integration pulls metrics)
                         Datadog ── dashboards + custom monitor queries
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
            Squadcast                  Slack
        (critical → page)      (#node-ops-alerts, warnings)
```

## Why Ops Agent + Datadog (vs Prometheus + Grafana)

The earlier design ran a self-managed **Prometheus** (scrape + storage) with **node_exporter**
and **Grafana** for dashboards. This one replaces all three self-hosted pieces with the Ops
Agent + Google's managed backends + Datadog. The reasons it's better here:

**One managed agent instead of three self-hosted services.** Ops Agent is GCP's first-party,
supported agent. It does host metrics, Prometheus scraping (it has a real `prometheus` receiver
with full `scrape_config`/relabeling), *and* logs in a single config — so node_exporter,
a standalone Prometheus server, and a separate log shipper all disappear. With Prometheus +
Grafana you operate, patch, secure, and monitor each of those yourself. Fewer services on a
security-sensitive validator host is a direct win.

**No self-managed storage.** Metrics land in Cloud Monitoring / GMP — a managed, scaled,
retained backend. There's no Prometheus TSDB to size, compact, back up, or keep alive (and a
local Prometheus dies exactly when the node host does). Grafana also needs *some* durable
metrics store behind it; here that's fully managed.

**Logs and metrics from the same agent.** Ops Agent ships both. The Prometheus + Grafana path
only does metrics — you'd bolt on Loki/Promtail (or similar) and wire it into Grafana to get
logs. One agent, one auth model, both signals.

**Keyless auth, smaller attack surface.** Ops Agent uses the VM's service account (ADC). No
scrape ports exposed publicly, no Grafana server to expose/secure, no API keys on the box.

**Datadog gives more than Grafana for the same data.** Grafana is dashboards only. Datadog is
dashboards **plus** alerting, no-data detection, multi-condition queries, on-call routing, and
correlation with the rest of the org's GCP infra in one pane of glass. Since the team already
uses Datadog, pulling these metrics in via the existing GCP integration avoids standing up and
maintaining a parallel Grafana stack.

> Honest tradeoffs the other way: Prometheus + Grafana is cloud-agnostic and fully PromQL-native
> with no per-metric SaaS bill; the Ops Agent prometheus receiver omits a few Prometheus
> features (service discovery, `honor_labels`) and is GCP-locked; and these series bill as both
> GMP samples and Datadog custom metrics. For a single GCE-hosted validator already in a Datadog
> shop, the lower operational surface outweighs the lock-in and cost — for a multi-cloud fleet
> the calculus can flip back toward Prometheus + Grafana.

## Layout

```
monitoring/
├── ops-agent/config.yaml         # the one VM agent: prometheus scrape + journald logs
└── datadog/
    ├── gcp-integration.md         # connect Datadog to GCP; include both metric namespaces
    ├── notifications.md           # wire up Squadcast + Slack
    └── monitors/                  # custom alert queries (apply via API/terraform)
        ├── 01_block_height_stalled.json   # crit  -> Squadcast   (gcp.prometheus.*)
        ├── 02_peer_count_low.json         # warn  -> Slack / crit -> Squadcast
        ├── 03_node_down.json              # crit/no-data -> Squadcast
        ├── 04_disk_usage.json             # warn -> Slack / crit -> Squadcast (gcp.agent.*)
        └── 05_cpu_memory.json             # warn -> Slack        (gcp.agent.*)
```

## Setup (order)

1. **Install the Ops Agent** on the VM and apply the config:
   ```bash
   curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
   sudo bash add-google-cloud-ops-agent-repo.sh --also-install
   sudo cp ops-agent/config.yaml /etc/google-cloud-ops-agent/config.yaml
   sudo systemctl restart google-cloud-ops-agent
   ```
   Give the VM service account `roles/monitoring.metricWriter` + `roles/logging.logWriter`.
   Verify in **Cloud Monitoring → Metrics Explorer**:
   `prometheus.googleapis.com/substrate_block_height/gauge` and `agent.googleapis.com/disk/percent_used`.
2. **GCP → Datadog.** Follow `datadog/gcp-integration.md` (include **both**
   `prometheus.googleapis.com` and `agent.googleapis.com` namespaces). Confirm `gcp.prometheus.*`
   and `gcp.agent.*` series in Datadog.
3. **Notifications.** Follow `datadog/notifications.md` for the Squadcast webhook + Slack.
4. **Monitors.** Apply the JSON (confirm exact metric names first):
   ```bash
   for f in datadog/monitors/*.json; do
     curl -s -X POST "https://api.${DD_SITE}/api/v1/monitor" \
       -H "DD-API-KEY: ${DD_API_KEY}" -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
       -H "Content-Type: application/json" -d @"$f" | jq '.id,.name'
   done
   ```

## Alerts — what, why, response, route

A validator's job is to **stay in sync and reachable**, so the queries watch those properties
plus the resource that most commonly kills the node. Sustained windows avoid flapping.

| Monitor | Source metric | Sev → route | Why / response |
|---|---|---|---|
| **Block height stalled** | `gcp.prometheus.substrate_block_height` | crit → Squadcast | "Up but useless". Check peers + db-sync lag, then logs; restart if wedged. |
| **Peer count low / zero** | `gcp.prometheus.substrate_sub_libp2p_peers_count` | warn → Slack / crit → Squadcast | Leading indicator — peers drop before height stalls. |
| **Node down / no-data** | `gcp.prometheus.up{job:midnight-node}` | crit → Squadcast | Process down or pipeline broken. Read logs before restart. Closes the off-VM-alerting gap. |
| **Disk usage** | `gcp.agent.disk.percent_used` | warn → Slack / crit → Squadcast | Most common real outage (db-sync fills disk). Prune/resize. |
| **CPU / memory** | `gcp.agent.memory.percent_used` (+ cpu variant) | warn → Slack | OOM/perf risk. Expected during initial db-sync, suspicious after. |

The three the brief requires are the first three rows; disk and cpu/memory are the resource examples.

## With more time

Commit the **Datadog dashboard JSON** here, manage monitors + the GCP integration via Terraform,
and add a **missed-slots** monitor once registered as a real FNO.
