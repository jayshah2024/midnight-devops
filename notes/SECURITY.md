# SECURITY — Key Management (Section 4)

**Threat model.** A registered signing key = network-trust credential.
Compromise: attacker signs as us (slashing, reputation, possible consensus impact).
Loss: we drop out of participation.
Goals: key never on a general-purpose host, every signature audited, rotation cheap enough to actually do.

## 1. Key storage

Rule: the signing key is a **signing oracle**, never a retrievable secret. Node sends data, gets a signature back; the private key never enters the VM.

| Option | How it holds the key | Use it for | Catch |
|---|---|---|---|
| **KMS (HSM-backed)** | non-exportable, IAM-gated `Sign` API, auto audit logs; keys are FIPS HSM-backed | the signing key | can't sign sr25519/ed25519 (see below) |
| **Secrets manager** | stores a *retrievable* secret → plaintext hits node memory | the long tail (DB creds, tokens, p2p seed) | weakest for signing; use Vault Transit if you must |

KMS is already HSM-backed, so no separate Cloud HSM tier is needed; for single-tenant/higher assurance use a **dedicated HSM protection level** key in the same KMS.

Curve reality for Midnight (partner-chains): cloud KMS signs RSA/ECDSA only.

| Key | Curve | KMS oracle? | If not |
|---|---|---|---|
| `partner_chains_key` | secp256k1 | yes (KMS HSM) | — |
| `aura` | sr25519 | no | Vault Transit / HSM / envelope-encrypt |
| `grandpa` | ed25519 | no | Vault Transit / HSM (AWS KMS signs ed25519 since 11/2025) |

Controls regardless of backend:
- IAM: only the node SA can `Sign`; humans get audit-read only; split `use` vs `rotate` roles.
- No keys in git, images, env files, or Terraform state. gitleaks in CI.
- Encrypt at rest + in transit. Audit every signature to an off-box immutable sink; alert on anomalies (unknown signer, off-hours, volume spikes).
- Disk encryption, locked-down SSH. Section 2 alerts double as a misuse signal.

## 2. Key rotation

The key is on-chain/governance state, so rotation is coordinated. Overlap, never gap.

1. Generate the new key in KMS (not exported).
2. Stage the new public key; old key stays live.
3. Register it via the network's key-update path (`setKeys`-style session rotation); time the cutover for the next session/era boundary.
4. Switch at the boundary; keep the old key until the new one is confirmed accepted.
5. Confirm signing on the new key, then schedule old-key destruction (no hard delete).
6. Log approver + old→new mapping.

Risks → mitigation:
- Participation gap → cut over on the era boundary, overlap keys.
- New key not yet accepted when old retired → never destroy until confirmed; keep a rollback window.
- Governance lag → announced maintenance window; watch Section 2 alerts through it.
- Fat-finger → rehearse in pre-prod, dry-run, four-eyes on the submission.

Cadence: scheduled hygiene, plus immediately on suspected compromise.

## 3. Incident response — suspected key exposure

First three actions:

1. **Contain.** Treat as compromised. Emergency-rotate; revoke the key's `Sign` ability (de-register, pull its IAM grant). First because every minute it's valid is a minute an attacker can sign as us.
2. **Coordinate.** Tell Midnight, the other FNOs, and security so they reject signatures from the old key and trigger consensus safeguards. Shared network = shared blast radius; disclosure is usually a governance obligation.
3. **Investigate.** Snapshot the host first (preserve forensics), then pull KMS/secrets and auth/signing logs: how it leaked, what got signed, whether other keys/hosts are hit.

Then: finish rotation, destroy the old key once the new is active, blameless post-mortem, close the gap (tighten IAM, move to a signing oracle if the key was ever file-resident, add the alert that should have caught it).
