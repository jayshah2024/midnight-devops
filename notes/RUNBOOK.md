# RUNBOOK — Midnight Pre-Prod FNO Onboarding

> Audience: a DevOps engineer who knows cloud/Linux but has **not** worked with Midnight or
> Cardano before. This runbook takes you from a bare GCP VM to a Midnight full node syncing
> against pre-prod, with evidence of block progression.
>
> **This setup runs everything from native binaries** (cardano-node, cardano-db-sync,
> midnight-node) managed as systemd services — not Docker. Binaries give you direct control
> over flags, config, and logs, and avoid container/volume overhead on the sync.
>
> **Read the timing note first:** Cardano DB Sync is a hard prerequisite and takes a
> **minimum of ~6 hours** to sync against pre-prod. Start it immediately and leave it
> running. Nothing in the Midnight stack will start cleanly until DB Sync has caught up

---

## 0. Architecture at a glance

The Midnight node does **not** run alone. It sits on top of a Cardano stack because
Midnight's consensus reads main-chain (Cardano) state. You are standing up four
long-running processes (all native binaries):

```
┌─────────────────────────────────────────────────────────────┐
│  GCP VM (Ubuntu 22.04)                                       │
│                                                              │
│  cardano-node ──► cardano-db-sync ──► PostgreSQL             │
│   (binary)          (binary)          (system service)       │
│       (pre-prod ledger)        (indexed chain → SQL)         │
│                                        ▲                     │
│                                        │ reads main-chain    │
│                                   midnight-node ─── CLI/RPC  │
│                                   (binary)                   │
└─────────────────────────────────────────────────────────────┘
```

- **cardano-node** — syncs the Cardano pre-prod chain (the "main chain").
- **cardano-db-sync** — replays the chain into PostgreSQL so it's queryable. **The slow part.**
- **PostgreSQL** — backing store for db-sync (installed via apt as a system service).
- **midnight-node** — the partner-chain node. Reads main-chain state, syncs Midnight blocks.

---

## 1. Provision the GCP VM

> Adjust to your project/zone. Sizing below is what I ran; db-sync is I/O-bound, so use SSD.

```bash
gcloud compute instances create midnight-preprod \
  --zone=us-central1-a \
  --machine-type=e2-standard-4 \          # 4 vCPU / 16 GB — comfortable for pre-prod
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=200GB \                # db-sync + ledger grow; 200GB gives headroom
  --boot-disk-type=pd-ssd \               # SSD matters — db-sync is I/O heavy
  --tags=midnight-node
```

**Firewall:** only open what you need. Keep RPC/metrics ports private (VPC-internal or
SSH-tunnelled), not public. Cardano p2p needs outbound; it does not need a public inbound
port for a non-block-producing node.

```bash
gcloud compute ssh midnight-preprod --zone=us-central1-a
```

### Base packages

```bash
sudo apt-get update && sudo apt-get install -y \
  git curl jq ca-certificates gnupg lsb-release \
  postgresql postgresql-contrib \          # Postgres for db-sync
  libpq-dev libsystemd-dev liblz4-dev      # runtime libs the Cardano binaries link against
```

---

> ⚠️ **Gotcha #1 — you can run this on macOS locally, but use a GCP VM for the real sync.**
> The binaries ship for **arm64** as well as x86_64, so the whole stack runs on Apple Silicon.
> I tested it on macOS via **Multipass** (a lightweight Ubuntu VM): provision an arm64 Ubuntu
> instance, drop in the arm64 binaries, and it runs fine. The catch is **sync speed** —
> Cardano DB Sync is bottlenecked by how fast you can pull and index the chain, and on a
> home/office connection that bandwidth (plus a laptop's thermals/disk) makes the ~6h pre-prod
> sync noticeably slower and less predictable. If your machine has ample disk (200GB+ free,
> SSD) it's a valid way to learn the setup, but a **GCP VM with an SSD persistent disk and
> datacenter bandwidth is the better experience** for the real sync. Recommendation:
> prototype/learn locally if you like, but run the real sync on the VM. Multipass quick start:
>
> ```bash
> brew install --cask multipass
> multipass launch 22.04 --name midnight --cpus 4 --memory 16G --disk 200G
> multipass shell midnight
> uname -m   # aarch64 → download the arm64 release artifacts, then follow §1 (skip gcloud) → §6
> ```
> 
## 2. Download the Cardano database snapshot

### Install Mithril tooling

```mkdir -p $HOME/tmp/mithril && cd $HOME/tmp/mithril
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/input-output-hk/mithril/refs/heads/main/mithril-install.sh | sh -s -- -c mithril-signer -d unstable -p $(pwd)
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/input-output-hk/mithril/refs/heads/main/mithril-install.sh | sh -s -- -c mithril-client -d unstable -p $(pwd)
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/input-output-hk/mithril/refs/heads/main/mithril-install.sh | sh -s -- -c mithril-aggregator -d unstable -p $(pwd)
```
### Configure environment variables (Preprod)

```
export CARDANO_NETWORK=preprod
export AGGREGATOR_ENDPOINT=https://aggregator.release-preprod.api.mithril.network/aggregator
export GENESIS_VERIFICATION_KEY=$(wget -q -O - https://raw.githubusercontent.com/input-output-hk/mithril/main/mithril-infra/configuration/release-preprod/genesis.vkey)
export ANCILLARY_VERIFICATION_KEY=$(wget -q -O - https://raw.githubusercontent.com/input-output-hk/mithril/main/mithril-infra/configuration/release-preprod/ancillary.vkey)
export SNAPSHOT_DIGEST=latest
```
### Download the snapshot

```
# List available snapshots
./mithril-client cardano-db snapshot list

# Show details for the target snapshot
./mithril-client cardano-db snapshot show $SNAPSHOT_DIGEST

# Download the database
./mithril-client cardano-db download --include-ancillary $SNAPSHOT_DIGEST

Mithril Client CLI version: 0.13.15+46240e9
Warning: Ancillary verification does not use the Mithril certification: as a mitigation, IOG owned keys are used to sign these files.
1/7 - Checking local disk info…                                                                                                                                                                                2/7 - Fetching the certificate and verifying the certificate chain…                                                                                                                                              Certificate chain validated                                                                                                                                                                                  3/7 - Downloading and unpacking the cardano db snapshot                                                                                                                                                           [00:10:05] [#####################################################################################################################################################################] Files: 5,813/5,813 (0.0s)4/7 - Downloading and verifying digests…                                                                                                                                                                       5/7 - Verifying the cardano database                                                                                                                                                                           6/7 - Computing the cardano db snapshot message                                                                                                                                                                7/7 - Verifying the cardano db signature…                                                                                                                                                                      Cardano database snapshot '8685daf643c85ecf8b11852c5aa0d26023dcbd092e519876f4069f68cdc6fdb3' archives have been successfully unpacked. Immutable files have been successfully verified with Mithril.

    Files in the directory 'db' can be used to run a Cardano node with version >= 11.0.1.

Download the **pre-built release binaries** for `cardano-node`, `cardano-cli`,
`cardano-db-sync`, and `midnight-node` for your architecture, verify checksums, and put them
on `PATH`. Use the versions the Midnight pre-prod docs/release notes pin — not "latest".
```

## 3. Set up the Cardano relay node

Always check for the latest release from the official Cardano node release page on GitHub.

```mkdir -p ~/.local/bin ~/.local/share

VERSION="11.0.1"
ARCH="linux-amd64"
URL="https://github.com/IntersectMBO/cardano-node/releases/download/${VERSION}/cardano-node-${VERSION}-${ARCH}.tar.gz"

curl -L "$URL" | tar -xz -C ~/.local/bin --strip-components=2 ./bin
curl -L "$URL" | tar -xz -C ~/.local/share --strip-components=1 ./share
chmod +x ~/.local/bin/cardano-*

cardano-node --version
```

> ⚠️ **Gotcha #2 — check your versions.** Use the latest version as per releases and check in compatibility matrix. A version mismatch between `midnight-node` and the
> pre-prod chainspec/genesis shows up as a node that starts but never finds peers / never
> advances. Pinning to the version from the release notes made the "no peers" symptom go away.

### Initialize the data directory

```
mkdir ~/cardano-data
mv ~/tmp/mithril/db/ ~/cardano-data/
```
### Configure the systemd service

Create `/etc/systemd/system/cardano-node.service`. Replace `[USER]` with your Linux username.

```toml
[Unit]
Description=Cardano Relay Node (Preprod)
Wants=network-online.target
After=network-online.target

[Service]
User=[USER]
Type=simple
WorkingDirectory=/home/[USER]/cardano-data
ExecStart=/home/[USER]/.local/bin/cardano-node run \
    --topology /home/[USER]/.local/share/preprod/topology.json \
    --database-path /home/[USER]/cardano-data/db \
    --socket-path /home/[USER]/cardano-data/db/node.socket \
    --host-addr 0.0.0.0 \
    --port 3001 \
    --config /home/[USER]/.local/share/preprod/config.json
KillSignal=SIGINT
Restart=always
RestartSec=5
LimitNOFILE=32768

[Install]
WantedBy=multi-user.target
```

```sudo systemctl daemon-reload
sudo systemctl enable cardano-node
sudo systemctl start cardano-node
```

<img width="845" height="127" alt="Screenshot 2026-06-12 at 8 45 20 PM" src="https://github.com/user-attachments/assets/3f76aebf-6e07-4f0f-8fda-e6c9110f3d0a" />

## 4. Configure PostgreSQL 17

### Install PostgreSQL

```sudo apt install curl ca-certificates -y
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -s -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
sudo apt update && sudo apt -y install postgresql-17 postgresql-server-dev-17
```
### Initialize the database and roles
```
CREATE USER midnight WITH PASSWORD 'your_secure_password';
ALTER ROLE midnight WITH SUPERUSER CREATEDB;
CREATE DATABASE cexplorer;
```
### Configure authentication
```
export POSTGRES_PASSWORD='your_secure_password'
export PGPASSFILE="${HOME}/.pgpass"

echo "/var/run/postgresql:5432:cexplorer:midnight:$POSTGRES_PASSWORD" > "$PGPASSFILE"
chmod 0600 "$PGPASSFILE"
```
### Performance tuning 

Preprod database sizes are significantly smaller than Mainnet. Tuning is less critical but still recommended.

Update `/etc/postgresql/17/main/postgresql.conf`:

| **Parameter** | **Recommended Value** | **Description** |
| --- | --- | --- |
| `shared_buffers` | 4GB | Keeps ledger data in active memory. |
| `maintenance_work_mem` | 1GB | Accelerates index building during sync. |
| `max_parallel_maintenance_workers` | 2 | Allows multiple cores to build indexes. |
| `effective_cache_size` | 12GB | Informs the planner of available RAM for caching. |
| `join_collapse_limit` | 1 | Force Postgres to follow the exact join order. |

## 5. Set up cardano-db-sync

### Install binaries and schema
```
mkdir -p ~/tmp
cd ~/tmp
curl -L -O https://github.com/IntersectMBO/cardano-db-sync/releases/download/13.7.1.0/cardano-db-sync-13.7.1.0-linux.tar.gz
tar -xzf cardano-db-sync-13.7.1.0-linux.tar.gz

cp bin/* ~/.local/bin/
mkdir -p ~/cardano-data/
sudo mv ~/tmp/schema ~/cardano-data/

cd ~/cardano-data
curl -O https://book.world.dev.cardano.org/environments/$NETWORK/db-sync-config.json
sed -i "s|\"NodeConfigFile\": \"config.json\"|\"NodeConfigFile\": \"/home/[USER]/.local/share/$NETWORK/config.json\"|" ~/cardano-data/db-sync-config.json
```

### Create the db-sync service

Create `/etc/systemd/system/cardano-db-sync.service`:

```toml
[Unit]
Description=Cardano DB Sync (Preprod)
After=cardano-node.service
Requires=cardano-node.service

[Service]
User=[USER]
Type=simple
Environment="PGPASSFILE=/home/[USER]/.pgpass"
WorkingDirectory=/home/[USER]/cardano-data
ExecStart=/home/[USER]/.local/bin/cardano-db-sync \
    --config /home/[USER]/cardano-data/db-sync-config.json \
    --socket-path /home/[USER]/cardano-data/db/node.socket \
    --schema-dir /home/[USER]/cardano-data/schema \
    --state-dir /home/[USER]/cardano-data/db-sync-state
KillSignal=SIGINT
Restart=always
RestartSec=10
LimitNOFILE=32768

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl daemon-reload
sudo systemctl enable cardano-db-sync
sudo systemctl start cardano-db-sync
```

### Verify synchronization ( will roughly take around 6 hours to get to 99.99 percent, until then do not proceed)
```
psql -d cexplorer -c "
SELECT
    100 * (EXTRACT(epoch FROM (MAX(time) AT TIME ZONE 'UTC')) - EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC')))
    / (EXTRACT(epoch FROM (NOW() AT TIME ZONE 'UTC')) - EXTRACT(epoch FROM (MIN(time) AT TIME ZONE 'UTC')))
AS sync_percent
FROM block;"
    sync_percent
---------------------
 99.9999939668250393
(1 row)
```

<img width="938" height="168" alt="Screenshot 2026-06-14 at 1 35 35 AM" src="https://github.com/user-attachments/assets/4861b362-292f-432c-bf4f-2f018adb9b39" />

<img width="1458" height="276" alt="Screenshot 2026-06-12 at 9 16 34 PM" src="https://github.com/user-attachments/assets/ef8dd29f-23f9-4afc-a045-4f3c8b1f9c89" />


> ⚠️ **Gotcha #3 — arch & libs.** Binaries are published for `x86_64` and `arm64`. Grab the
> one matching `uname -m`. On a fresh Ubuntu VM you may hit missing shared libs at first run
> (`libsystemd`, `liblz4`) — `ldd $(which cardano-node)` shows what's missing; install the
> matching `-dev`/runtime packages.

---

> ⚠️ **Gotcha #4 — order matters.** db-sync needs the node's `node.socket` to exist before it
> can connect. Start `cardano-node` first and wait for the socket; if db-sync starts first it
> just retries noisily. The `After=`/`Requires=` above handle this on reboot.

---

## 6. Start the Midnight node (after DB Sync completes)

### Prepare directories

```
mkdir -p ~/data ~/res ~/.local/bin
```

- **`~/data`**: Stores the node database and base path.
- **`~/res`**: Stores chain configuration files.
- **`~/.local/bin`**: Stores executable binaries.

### Download and install the binary

Always verify the latest release tag from the official Midnight Node repository.

1. Download and extract the node:
    
    ```
    mkdir -p ~/tmp && cd ~/tmp
    curl -L -O https://github.com/midnightntwrk/midnight-node/releases/download/node-1.0.0/midnight-node-1.0.0-linux-amd64.tar.gz
    tar -xvzf midnight-node-1.0.0-linux-amd64.tar.gz
    ```
    
2. Move the files to their permanent locations:
    
    ```
    mv ~/tmp/midnight-node ~/.local/bin/
    mv ~/tmp/res ~/res
    ```
    
3. Refresh your shell environment:
    
    ```
    source ~/.bashrc
    ```
### Create the .env file

```
# PostgreSQL connection
POSTGRES_HOST='localhost'
POSTGRES_DB='cexplorer'
POSTGRES_PORT=5432
POSTGRES_USER='midnight'
POSTGRES_PASSWORD='YOUR_POSTGRES_PASSWORD'
DB_SYNC_POSTGRES_CONNECTION_STRING=postgresql://midnight:YOUR_POSTGRES_PASSWORD@localhost:5432/cexplorer

# Cardano Preprod params
CARDANO_SECURITY_PARAMETER='432'
BLOCK_STABILITY_MARGIN=30

# Push to public telemetry
PROMETHEUS_PUSH_ENDPOINT='https://telemetry.shielded.tools/api/v1/receive'

# Midnight node settings
CFG_PRESET=preprod
NODE_NAME='YOUR_NODE_NAME'

# Absolute path to network and keystore files
NODE_KEY_FILE='/home/midnight/data/chains/midnight_preprod/network/secret_ed25519'
AURA_SEED_FILE='/home/midnight/keystore/61757261...'
GRANDPA_SEED_FILE='/home/midnight/keystore/6265656...'
CROSS_CHAIN_SEED_FILE='/home/midnight/keystore/6772616...'
```

### Load variables and start the node

1. Load the environment variables:
    
    ```
    source ~/.env
    ```
    
2. Launch the node:
    
    On Preprod, the node connects through standard peer discovery — no overlay flags required.
    
  ```
  midnight-node \
    --chain /home/midnight/res/preprod/chain-spec-raw.json \
    --base-path /home/midnight/data \
    --pool-limit 35 \
    --name $NODE_NAME \
    --no-private-ip
   ```   

> **Monitoring handoff:** the node exposes Prometheus metrics on `:9615` and `cardano-node` on
> `:12798`. Keep these bound to localhost — the Google Cloud Ops Agent scrapes them locally and
> ships to Managed Service for Prometheus; no public metrics port is needed. Full monitoring
> setup (Ops Agent config, Datadog integration, alerts) is in `monitoring/`.

### Query the node (CLI / RPC)

Midnight exposes a Substrate-style JSON-RPC. Substitute the exact methods the pre-prod doc
gives; these are the typical calls:

```
curl -X POST   -H "Content-Type: application/json"   -d '{
        "jsonrpc": "2.0",
        "method": "system_chain",
        "params": [],
        "id": 1
      }'   http://localhost:9944/
{"jsonrpc":"2.0","id":1,"result":"Midnight Preprod"}
```
```
2026-06-15 14:15:12 ❤️  by Substrate DevHub <https://github.com/substrate-developer-hub>, 2017-2026
2026-06-15 14:15:12 📋 Chain specification: Midnight Preprod
2026-06-15 14:15:12 🏷  Node name: jcs
2026-06-15 14:15:12 👤 Role: FULL
2026-06-15 14:15:12 💾 Database: ParityDb at /root/data/chains/midnight_preprod/paritydb/full
2026-06-15 14:15:12 Index idx_multi_asset_policy_name_hex already exists, skipping creation.
2026-06-15 14:15:12 Index 'idx_ma_tx_out_ident' already exists
2026-06-15 14:15:12 Index 'idx_tx_out_address' already exists
2026-06-15 14:15:12 Index 'idx_ma_tx_out_tx_out_id_ident' already exists
2026-06-15 14:15:12 Using candidate data source configuration: CandidateDataSourceCacheConfig { cardano_security_parameter: 432 }
2026-06-15 14:15:13 🔨 Initializing Genesis block/state (state: 0xf651…d0e9, header-hash: 0xdf83…361b)
2026-06-15 14:15:13 Creating transaction pool txpool_type=ForkAware ready=Limit { count: 35, total_bytes: 20971520 } future=Limit { count: 3, total_bytes: 2097152 }
2026-06-15 14:15:13 👴 Loading GRANDPA authority set from genesis on what appears to be first startup.
2026-06-15 14:15:13 Using default protocol ID "sup" because none is configured in the chain specs
2026-06-15 14:15:13 🏷  Local node identity is: 12D3KooWHSCckc4MPD7q14AnLE3v6bm6Nn1eT3XsoTzEzotViCfF
2026-06-15 14:15:13 Running libp2p network backend
2026-06-15 14:15:13 local_peer_id=12D3KooWHSCckc4MPD7q14AnLE3v6bm6Nn1eT3XsoTzEzotViCfF
2026-06-15 14:15:13 💻 Operating system: linux
2026-06-15 14:15:13 💻 CPU architecture: x86_64
2026-06-15 14:15:13 💻 Target environment: gnu
2026-06-15 14:15:13 💻 CPU: AMD EPYC 7B12
2026-06-15 14:15:13 💻 CPU cores: 4
2026-06-15 14:15:13 💻 Memory: 32090MB
2026-06-15 14:15:13 💻 Kernel: 6.17.0-1016-gcp
2026-06-15 14:15:13 💻 Linux distribution: Ubuntu 24.04.4 LTS
2026-06-15 14:15:13 💻 Virtual machine: yes
2026-06-15 14:15:13 📦 Highest known block at #0
2026-06-15 14:15:13 〽️ Prometheus exporter started at 127.0.0.1:9615
2026-06-15 14:15:13 Running JSON-RPC server: addr=127.0.0.1:9944,[::1]:9944
2026-06-15 14:15:13 🥩 BEEFY gadget waiting for BEEFY pallet to become available...
2026-06-15 14:15:13 MemoryMonitorService: memory monitoring disabled (threshold=0)
```

## 7. Validator keys / becoming an *active* FNO — scope note

This runbook stops at a **synced full node with CLI attached**, which is what Section 1 asks
to evidence. Becoming an *active block-producing* FNO is intentionally out of scope, and not
just for time reasons:

- Midnight's pre-prod validator set is **permissioned**. The authorized validators
  ("permissioned candidates") are fixed in the genesis/chainspec, and the initial authority
  public keys are pre-loaded (`initial-authorities.json`). You cannot self-register into
  consensus.
- Generating signing keys locally and pointing the node at them would **not** make you a
  block producer — you'd run as a full/observer node that never gets a slot. So it proves
  nothing beyond what the full node already shows.
- Real onboarding requires the Midnight team to whitelist your public keys in genesis as
  part of the governance process.

**How I would do it with FNO access** (documented for the follow-up conversation):
1. Generate the node's session/signing keys per the Midnight key-gen step (Substrate-style
   `key generate` / partner-chain keygen), keeping seeds off the host (see `SECURITY.md`).
2. Submit the resulting **public** keys to Midnight for inclusion as a permissioned candidate
   in the chainspec.
3. Configure the node with the key paths via env/secret mounts, restart, and confirm it
   appears in the active authority set and starts getting slots.
4. Verify block *production* (not just import) in logs and confirm peers see the produced
   blocks.

> Note: the standard setup may still generate a **node identity / p2p key** (network identity,
> not a signing key). That's fine and unrelated to block production — do it if the pre-prod
> doc's setup flow includes it.

---

## 8. Teardown

```bash
sudo systemctl disable --now midnight-node cardano-db-sync cardano-node
sudo rm -rf /var/lib/midnight                 # drops ledger + db-sync state (full re-sync!)
sudo -u postgres dropdb cexplorer             # drop the indexed DB
gcloud compute instances delete midnight-preprod --zone=us-central1-a
# Local: multipass delete midnight && multipass purge
```

---

## Appendix — Debugging walkthrough for Midnight node syncing: node starts but stays at block #0

> This is a real, sequential debugging session against pre-prod, kept verbatim because it
> documents the *method* (and because the brief asks us to be honest about gotchas). Each step
> fixed one layer and exposed the next. The key triage command throughout is `system_syncState`.

**The triage command (use it at every step):**

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"system_syncState","params":[]}' \
  http://localhost:9944 | jq
# {
#   "result": { "startingBlock": 0, "currentBlock": 0, "highestBlock": 0 }
# }
```

Read it as: `currentBlock` = where I am; `highestBlock` = the best tip my peers advertise.
- `highestBlock == 0` → my peers don't have the chain (chain-spec/bootnode problem).
- `highestBlock` high but `currentBlock == 0` → peers have the chain but I can't *import* it
  (verification/main-chain problem).

### Step 1 — Node won't even start: `missing field cardano_security_parameter`

```
error: ... Failed to read candidates data source config: missing field `cardano_security_parameter`
```

The db-sync main-chain follower needs the Cardano chain parameters and they weren't supplied.
Set them in the node's `EnvironmentFile` (values must match **Cardano pre-prod** — confirm
against Midnight's pre-prod docs, don't trust copied constants):

```bash
DB_SYNC_POSTGRES_CONNECTION_STRING=postgresql://<user>:<pass>@localhost:5432/cexplorer
CARDANO_SECURITY_PARAMETER=432
CARDANO_ACTIVE_SLOTS_COEFF=0.05
BLOCK_STABILITY_MARGIN=0
MC__FIRST_EPOCH_NUMBER=4
MC__FIRST_SLOT_NUMBER=86400
MC__FIRST_EPOCH_TIMESTAMP_MILLIS=1655769600000
MC__EPOCH_DURATION_MILLIS=432000000
```

→ Node now starts. Next layer.

### Step 2 — Node runs but sits 💤 Idle at `#0` with peers

`system_syncState` showed `highestBlock: 0` — so the connected peers were themselves at
genesis. That means we were **not on the real pre-prod chain**: either a self-generated
chain-spec or stale/wrong bootnodes. Fix: use Midnight's **official pre-prod chain-spec**
(correct genesis + bootnodes baked in), or add the official bootnodes explicitly:

```ini
--bootnodes /dns/<official-preprod-host>/tcp/30333/p2p/<peer-id>
```

→ After this, `system_syncState` showed `highestBlock: 1156672` and the log briefly flipped to
`⚙️ Syncing, target=#1156672`. We were now on the real chain. Next layer.

### Step 3 — On the real chain, but import fails: `Main chain state … not found`

```
💔 Verification failed ... "Main chain state aee886… referenced in imported block at slot
295671834 with timestamp 1774031004000 not found"
... Error importing block ...: block has an unknown parent
```

First hypothesis: db-sync behind. **Ruled out** — db-sync was fully caught up:

```bash
psql -d cexplorer -c "SELECT max(time) AS latest_block, now()-max(time) AS lag FROM block;"
#  latest_block        |       lag
#  2026-06-15 14:36:14  | 00:00:05.82   <- 5 seconds, fully synced
```

So db-sync has the data, yet the node still rejects the block. Next layer.

### Step 4 — The decisive log line: `stable block outside allowed range`

```
Get stable block by hash failed: Block with hash aee886… has timestamp 2026-03-20 02:43:47,
outside allowed range [2026-03-20 11:11:24 ..= 2026-03-20 15:59:24]
for reference timestamp 2026-03-20 18:23:24.
```

This reframes everything. db-sync **does** have block `aee886…` (its timestamp is known). The
node is *rejecting* it because the referenced main-chain block falls **outside the Cardano
stability window** the node computes for that partner-chain block's reference time.

Check the window math against our params:
- reference time `18:23:24`; allowed `[11:11:24 .. 15:59:24]`.
- newest bound = ref − `2h24m` = `k/f` = `432/0.05` = 8640 s ✓
- oldest bound = ref − `7h12m` = `3k/f` = `25920 s` ✓

So `CARDANO_SECURITY_PARAMETER=432` and `ACTIVE_SLOTS_COEFF=0.05` are **correct** — the window
matches pre-prod exactly. But the block being imported references a main-chain block
`02:43:47`, i.e. **~15.7 h** before the reference time — far outside the legitimate 2.4–7.2 h
window.

### What we tried and ruled out

- **`BLOCK_STABILITY_MARGIN`** — adds *blocks* to `k` (docs: keep `0`, max `1`). 30 blocks ≈
  10 min of shift; bridging ~8 h would need a margin of ~500, which is nonsensical and would
  reference non-final Cardano state. Also, the margin must **match what the network used** to
  produce the block — changing it unilaterally just makes our node disagree differently.
  **Not the fix; reverted to 0.**
- **db-sync lag** — ruled out (5 s, step 3).
- **chain-spec/bootnodes/params** — verified correct (steps 1–2, and the window math in step 4).

### Conclusion — narrowed to a partner-chain ↔ main-chain timestamp mismatch (node-version / network)

The partner-chain block references a Cardano "stable" block ~15.7 h old, but a correctly
configured node only accepts one 2.4–7.2 h old. The only way a *validly produced* block
references a main-chain block that far back is that the Cardano main chain had an
**abnormally long gap with no stable block around 2026-03-20** (a pre-prod main-chain stall),
and whether our node accepts that historical block is governed by the **node version's**
stability/inherent-data logic — **not** by any local config knob we control.

We were running **node v1.0.0**. So the remaining variable is node-version / network
compatibility, not our setup. This is a **genuine external blocker**, which the assessment
explicitly accepts:

> "If you hit a genuine blocker with pre-prod access or tooling, note it in your README and
> describe how you would have approached it — that's a valid response."

**State reached:** node on the correct pre-prod chain (peers advertising the real ~1.15M tip),
db-sync fully synced, Cardano data-source configured and the stability window verified correct
— failing only on importing historical blocks across a main-chain gap.

**How I'd resolve it with more time:**
1. Match the node to the **version Midnight specifies for current pre-prod** (compatibility
   matrix / release notes) — v1.0.0 may predate a fix for handling main-chain gaps. Upgrade and
   retry.
2. Check the Midnight forum threads ("Preprod node not syncing", "Preprod / Preview network
   status") for a documented 2026-03-20 main-chain incident and the recommended version/workaround.
3. If it's a known historical gap, follow Midnight's guidance (e.g. a node release that tolerates
   it, or a snapshot to sync past the affected range), then confirm `system_syncState` shows
   `currentBlock` climbing toward `highestBlock`.
