# Datadog ↔ Google Cloud integration (metrics path)

This connects Datadog to the GCP project so that the Managed Service for Prometheus (GMP)
metrics the VM pushes — stored in Cloud Monitoring under `prometheus.googleapis.com/*` —
become queryable in Datadog. No Datadog Agent runs on the node; Datadog *pulls* from Cloud
Monitoring.

## 1. Connect the project (Workload Identity Federation — keyless, preferred)

Datadog's modern GCP integration uses WIF instead of a downloaded SA key. In Datadog:
**Integrations → Google Cloud Platform → Add GCP Account**, which gives you the Datadog
principal + the steps. Equivalent gcloud:

```bash
PROJECT_ID=<your-project>

# Service account Datadog will impersonate.
gcloud iam service-accounts create datadog-integration \
  --project="$PROJECT_ID" --display-name="Datadog integration"

SA="datadog-integration@${PROJECT_ID}.iam.gserviceaccount.com"

# Read-only roles Datadog needs to pull metrics + resource metadata.
for ROLE in roles/monitoring.viewer roles/compute.viewer \
            roles/cloudasset.viewer roles/browser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="$ROLE"
done

# Bind Datadog's WIF principal to impersonate the SA (values come from the Datadog UI).
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/<DD_WIF_PROJECT>/locations/global/workloadIdentityPools/<DD_POOL>/*"
```

## 2. Make sure the right metric namespaces are collected

This is the key step for this design — by default the GCP integration pulls GCP *service*
metrics; the Ops Agent writes to two namespaces you must include (and these count as Datadog
custom metrics):

- **App metrics (Prometheus receiver → GMP):** `prometheus.googleapis.com` — the node series
  (block height, peers). In Datadog these appear as **`gcp.prometheus.<metric_name>`**
  (e.g. `gcp.prometheus.substrate_block_height`).
- **Host metrics (Ops Agent system metrics):** `agent.googleapis.com` — CPU/mem/disk. In
  Datadog these appear under **`gcp.agent.*`** (e.g. `gcp.agent.disk.percent_used`,
  `gcp.agent.memory.percent_used`, `gcp.agent.cpu.utilization`).

Steps:
- In the Datadog GCP integration tile, under **Metric collection**, ensure **both**
  `prometheus.googleapis.com` and `agent.googleapis.com` are **not excluded** (include them
  explicitly via the namespace filters).
- Confirm the VM is exporting: Cloud Monitoring → Metrics Explorer should show
  `prometheus.googleapis.com/substrate_block_height/gauge` and
  `agent.googleapis.com/disk/percent_used`.
- After ~10–15 min the same series appear in Datadog's Metrics Explorer. **Confirm the exact
  Datadog metric names** there and update the monitor JSON in `monitors/` if they differ —
  the `gcp.agent.*` / `gcp.prometheus.*` names above are the expected mapping, not guaranteed.

## Cost / cardinality note (be deliberate)

GMP bills per sample ingested, and these series also land as Datadog **custom metrics**, which
are billed by volume. The VM's `prometheus.yml` already uses a 30s scrape interval and only
the three jobs we actually dashboard/alert on; keep label cardinality low (no per-request or
per-peer labels) to keep both bills predictable.
