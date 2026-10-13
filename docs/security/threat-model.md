# Threat model and abuse resistance

This doc lists the adversaries Potluck designs against and the mitigations for each. It is a companion to `docs/phase-g/mesh-design.md`.

Each mitigation carries one label:

- **Implemented**: present in the code in this repository. The text names the file or endpoint.
- **Planned**: not built. It describes an intended design.

Re-read this doc whenever a feature touches identity, credit accounting or peer-to-peer connectivity.

---

## Adversary model

Potluck designs against five adversaries. Mitigations differ for each.

| # | Adversary | Goal | Typical capability |
|---|---|---|---|
| **A1** | **Economic gamer.** A user who wants more from Potluck than they contribute: free priority routing, an inflated credit balance, more benefits than their contribution earns. | Maximize personal return. Not malicious to others, but extractive to the network. | Standard user. May run multiple accounts, multiple machines and automated scripts. |
| **A2** | **Cheating contributor.** A peer operator who claims to serve quality inference but returns garbage, runs a smaller model than advertised, or fakes uptime. | Earn credits for less work than claimed. | Controls their own peer and can modify the contributor sidecar in any way. |
| **A3** | **Hostile requester.** An attacker sending malicious prompts through the network to abuse contributors. | Exhaust a target's GPU, extract model behavior, test jailbreaks, deanonymize peers. | Standard member account, or coordinator-side API access. |
| **A4** | **Compromised peer or malicious household member.** A peer machine that has been compromised (malware, supply chain, or a trusted but untrustworthy person with access). | Lateral movement on the household mesh, escalation to other household machines, exfiltration. | Has a shell on the compromised peer and sees that peer's WireGuard private key. |
| **A5** | **Insider or coordinator compromise.** A person with access to the Potluck infrastructure, or an attacker who breached it. | Manipulate the credit ledger, redirect inference jobs, dump member identities. | Read and write access to the coordinator's database and control plane. |

Each mitigation below says which adversary it addresses. A defense that closes A1 does not necessarily close A5.

---

## Trust boundaries

- **Inside the trust boundary:** the user's own machines, the coordinator and DERP relay infrastructure the Potluck operator runs, and the integrity of the signed Potluck installer.
- **Outside the trust boundary:** anything a peer's contributor sidecar reports about itself (output tokens, latency, served model), anything one peer says about another, any prompt content from a requester, and any model output from a contributor.
- **Soft boundary (verify, do not trust):** member identity (it can be socially engineered), contributor uptime claims (checked against the signaling channel), and coordinator-issued JWTs (signatures are verified).

Anything a peer or member self-reports is cross-checked against an authoritative observer, the coordinator, before it affects credits or routing.

**Blinded routing (Implemented).** The coordinator never puts a member id, conversation id or other requester identifier into a message sent to a peer (`JobAssignedMessage` in `services/coordinator/coordinator/schemas/peer.py`). `services/coordinator/tests/test_privacy_invariants.py` and `scripts/dev/lint_privacy.py` pin this.

---

## Threat category 1: Sybil attacks on membership and credit accrual (A1, A2)

### Scenario

A single person creates 50 accounts, runs 50 contributor peers on 50 rented VMs, and claims 50 times the credit balance and 50 times the priority-routing benefit. The network credits more than the actual work justifies.

### Mitigations

- **Implemented. Coordinator-authoritative credit ledger.** Credits are written only by the coordinator when a job reaches a completed outcome with output tokens above zero (`_finalize_job_in_db` in `routers/jobs.py`, `services/credit_ledger.py`). The coordinator counts the tokens it relays and bills the smaller of its own count and the job's `max_tokens` (`routers/peers.py`). A peer cannot write its own ledger row.
- **Implemented. Failed-job filtering.** Timed-out, cancelled, errored and dispatch-failed jobs write no credits.
- **Implemented. No credit for serving yourself.** A job served by a machine owned by the requester writes no ledger rows, so owning both ends of a job mints nothing.
- **Implemented. Escrow for cross-member earnings.** Credits earned from open-pool jobs are held out of the spendable balance until the serving peer passes canary checks (see category 2). Escrow is skipped when the peer is already trusted.
- **Implemented. Device-key binding.** At attestation, each device key is pinned to one member. A known key cannot later attest as a different member. The `require_registered_keys` setting turns this into a strict allowlist of registered keys (`services/attestation.py`).
- **Planned. Hardware fingerprinting per node.** Recording a hardware fingerprint at enrollment so that many VMs on one host form an identifiable pattern. Nothing in the code collects this today.
- **Planned. Anomaly review.** Flagging contributors whose credit trajectory looks unusual, such as a sudden jump or several accounts behind shared IP ranges.

### Residual risk

An adversary who controls many real identities can still run this attack. Detection depends on the planned anomaly review and on the canary and escrow checks, which bound how much a fabricating peer can earn.

---

## Threat category 2: Quality gaming by contributors (A2)

### Scenario A: Garbage tokens

A cheating contributor accepts a job, returns 200 tokens of repeated words, and collects credit for 200 tokens times the model-class multiplier.

### Scenario B: Model spoofing

A contributor advertises a 72B model (a high multiplier) but serves a 3B model (a low multiplier) and earns the higher rate.

### Scenario C: Slow-token padding

A contributor runs the right model but slows token emission to inflate `duration_ms`. Credit uses `output_tokens`, so duration does not change the credit math.

### Mitigations

- **Implemented. Canary probes.** The coordinator sends peers generated canary jobs at temperature 0: a random code to echo back or a small arithmetic question, with a known answer and a randomized token budget (`services/verification.py`). The prompts are generated, so there is no fixed list to memorize. A pass raises the peer's reputation by one step. A wrong answer resets reputation to 0. A timeout or delivery failure lowers it by one step. Probes use the same message path as real jobs and write no ledger rows.
- **Implemented. Reputation-gated escrow release.** A peer needs about five clean canaries before its escrowed earnings are released (`services/credit_ledger.py`). A confirmed fabrication starts the climb over.
- **Implemented. Open-pool reputation gate.** Open-pool dispatch drops peers whose reputation is below the neutral bar. A peer that hit 0 stays out until clean canaries lift it back to neutral (`services/peer_registry.py`). Routing to your own machines does not use this gate.
- **Implemented. Quality scoring from member feedback, coordinator side.** `POST /jobs/{job_id}/feedback` records a thumbs-up or thumbs-down against the serving peer and model. Scores are a recency-weighted mean. Three consecutive failures remove a peer from dispatch, and one passing score lifts the exclusion. The peer and the requester never see the scores. See [peer-quality.md](peer-quality.md).
- **Planned. A rating control in the desktop app.** No desktop UI calls the feedback endpoint yet, so the signal is not collected from real use.
- **Implemented. Peer-reported token counts do not set credits.** Billing uses the coordinator's own count of relayed tokens, capped at the job's `max_tokens`. A peer claim above the coordinator's count is logged as a fraud signal (`routers/peers.py`).
- **Planned. Model fingerprinting at enrollment.** Running a fixed prompt against a newly registered capability and storing the response signature.
- **Planned. Model-substitution detection.** The sidecar exposes `POST /verify/score` (a teacher-forced log-probability), and the coordinator contains an observe-only detector for a cheaper substitute model (`services/closure_detector.py`). Nothing connects the two to reputation, routing or credits yet.
- **Planned. Token-rate sanity check.** Comparing a peer's observed token rate with the distribution for its claimed model class. Dispatch uses the peer's self-reported `tokens_per_second` today and does not cross-check it.

### Residual risk

A contributor who runs the right model but returns deliberately low-quality output can still earn credit until feedback-based scoring catches it. That signal is slow and depends on the planned rating control.

---

## Threat category 3: Hostile prompts (A3)

### Scenario A: Resource exhaustion

An attacker requests a very large `max_tokens` to tie up a contributor's GPU.

### Scenario B: Prompt injection across contributors

An attacker crafts prompts that elicit jailbreak text and compares outputs across contributors to fingerprint which model each runs.

### Scenario C: Costly-prompt griefing

An attacker sends many maximum-length jobs aimed at one or two contributors to burn their electricity and hardware.

### Mitigations

- **Implemented. Hard caps on sampling parameters.** `max_tokens` is at most 4096 (default 512) and `temperature` is between 0 and 2. The coordinator validates both before it sends a job (`SamplingParams` in `schemas/peer.py`). The job schema has no `n` parameter, so each job produces one completion.
- **Implemented. Job time limits.** A stream has a 120-second limit after dispatch, and a first-token deadline that scales with prompt size and the peer's measured throughput (`routers/jobs.py`).
- **Implemented. Load-aware dispatch.** The peer score is divided by one plus the peer's in-flight jobs, which spreads load. A contributor can also set `max_concurrent_jobs` (0 to 64) so the coordinator stops dispatching to it once it is full.
- **Implemented. Contributor opt-in.** A machine serves inbound jobs only while contributor mode is on. `/peer/chat/completions` returns 503 otherwise, and the contribution schedule can limit serving hours.
- **Planned. Per-member rate limiting.** A cap on concurrent and hourly jobs per member. The coordinator has no such limit today.
- **Planned. Prompt size limit.** A maximum prompt length enforced at the coordinator.
- **Planned. Dispatch backoff per requester.** Limiting consecutive jobs from one requester to one contributor.
- **Planned. Contributor-side cost ceiling per requester.** Throttling a requester and contributor pairing that consumes a large share of the contributor's capacity.
- **Planned. Optional content filter for contributors.** A local, category-level filter, with the coordinator routing around contributors who refuse a job.

### Residual risk

Prompt injection that arrives in legitimate-looking prompts is hard to detect ahead of time. Detection relies on after-the-fact signals such as quality scoring and reports.

---

## Threat category 4: Mesh penetration and lateral movement (A4)

### Scenario

An attacker compromises a peer machine, for example through phishing, malware and privilege escalation, and reads the WireGuard private key from disk. The compromised peer is now a trusted member of the household mesh and can be used to reach other household machines.

### Mitigations

- **Implemented. Minimal local control surface.** The mesh sidecar is controlled through a Unix socket or Windows named pipe restricted by file permissions or a DACL, not a TCP port. See [local-ipc.md](../phase-g/local-ipc.md).
- **Implemented. Peer-facing surface limits.** The Python engine listens on all interfaces so household machines can reach it. Callers that are not on the same machine can reach only `/peer/*` and `/health`. `/peer/*` also requires that the source address is a reachable machine in the local mesh sidecar's household roster, with a recent WireGuard handshake, and that the connection arrived on this machine's own mesh address (`services/peer_trust.py`). Memory, tasks, chat history and agent observability are reachable only from the machine itself.
- **Implemented. Tunnel packet filter.** The TUN packet filter passes traffic only for addresses the validated household roster holds (`apps/desktop/go/potluck-mesh/cmd/potluck-mesh/packetfilter.go`).
- **Implemented. Per-node revocation.** A member can remove a machine from their household (`DELETE /mesh/households/current/machines/{node_id}`). The coordinator deletes the node, ends direct circle links to or from it, and broadcasts a roster update so online household machines drop it.
- **Planned. Rotation of static keys.** WireGuard rotates its session keys automatically. Rotation of the node's static WireGuard key and the Ed25519 signing key is not built.
- **Planned. Audit log of inter-node connections.** A coordinator log of which node tried to reach which, for spotting mass-connection patterns.
- **Planned. Service-level ACLs on the mesh.** Limiting which mesh services each node can reach, so a compromised contributor machine cannot reach a member's primary workstation.

### Residual risk

Significant. Once a peer is compromised it is trusted within the household. It can deny service to other household machines and consume their capacity. The mesh layer does not protect against this. The member protects their machine with endpoint security.

---

## Threat category 5: Pairing token theft (A1, A4)

### Scenario

A user generates a pairing token to add a second machine. The token leaks (a screenshot in a chat, a screen recording, interception before the second machine uses it). An attacker enrolls a malicious node into the household.

### Mitigations

- **Implemented. Single-use tokens.** A pairing token is consumed by the first successful enrollment.
- **Implemented. One-hour lifetime.** Tokens expire one hour after issue. A token is 20 random bytes encoded as base32 (`models/pairing.py`, `routers/mesh.py`).
- **Implemented. LAN pairing does not broadcast the token.** A machine inviting another on the local network announces only a one-way fingerprint of the token. The joining machine needs a confirmation PIN shown on the inviter's screen to obtain the token, and the inviter ends the session after a small number of wrong PINs (`apps/desktop/go/potluck-mesh/internal/lanpair`, `POST /pairing/reveal`).
- **Implemented. Visible roster.** Household machines are listed in the My Machines view, so an unexpected machine can be seen and removed.
- **Planned. Confirmation on existing machines.** A prompt on the household's other online machines when a new device enrolls, with a decision window after which the enrollment is reverted.
- **Planned. Platform-bound tokens.** A token that records the expected platform of the machine being added.

### Residual risk

A token that is exposed during its one-hour window can get an attacker enrolled until the owner notices the extra machine and removes it. The remaining protection is the owner's attention to the roster.

---

## Threat category 6: Coordinator insider threat (A5)

### Scenario

A person with read and write access to the coordinator's database, or an attacker who has breached it, could:
- Inflate their own credit-ledger balance.
- Redirect inference jobs to their own peer to collect credits.
- Read member identities and link a member id to a real identity.

### Mitigations

- **Implemented. Hash-chained credit ledger.** Each ledger row stores `prev_row_hash` and `row_hash` (SHA-256), chained per member starting from a fixed genesis value (`services/credit_ledger.py`). A member can fetch their own rows with `GET /me/credit-history` and check that the chain is intact. The coordinator cannot rewrite history without breaking the chain. The `escrowed` flag is not part of the hash.
- **Implemented. Public fleet aggregates.** `GET /transparency/fleet` returns counts only (active and online peers, platform mix, version distribution). It carries no member or peer identifiers.
- **Implemented. Reviewable source.** The coordinator source is in `services/coordinator`, so reviewers can check what the service is supposed to do.
- **Planned. Verifiable routing decisions.** Recording each dispatch decision with a Merkle proof so members can audit the routing history of their jobs afterward.
- **Planned. Database-level access auditing and alerting** on privileged queries, such as writes to the ledger or changes to household ownership.

### Residual risk

A compromise that lasts long enough can still do meaningful damage. The ledger chain makes tampering with past rows detectable by anyone who checks, but it does not prevent routing manipulation or identity disclosure. While the coordinator is a central service, an insider with enough access has significant power.

---

## Threat category 7: DERP relay compromise (A5-adjacent)

### Scenario

An attacker compromises a DERP relay and wants to observe traffic.

### Mitigations

- **Implemented. End-to-end WireGuard encryption.** A relay sees encrypted packets and the source and destination WireGuard public keys. It does not see plaintext.
- **Implemented. Authenticated relay access.** The relay requires an HS256 JWT issued by the coordinator, with the claims `wg_pubkey`, `region`, `exp` and `iat` and no member, household or node identity. The `region` claim must match the relay's region. Tokens last one hour (`routers/mesh.py`, `services/derp`).
- **Implemented. Per-client rate limit.** The relay drops packets above 1000 per second per client. It never logs payloads, and logs only the first 8 characters of a public key.
- **Implemented, optional by default. Proof of possession for relay tokens.** When a node presents a nonce and an Ed25519 signature with its token request, the coordinator verifies it and rejects a bad signature. A request without them is accepted unless the `derp_pop_required` setting is on.
- **Planned. Multiple relay regions.** A single relay region sees all relayed traffic, so one compromised relay observes all of it. Several regions would limit what any one relay sees.
- **Planned. Traffic-shape protection.** Packet-size padding or cover traffic against timing and size analysis.

### Residual risk

A compromised relay can do timing and size correlation on the traffic it carries, and Potluck has no countermeasure for that today. The relay validates tokens with the same HMAC key the coordinator signs them with, so a compromised relay also holds the key and can mint tokens that other relays using that key would accept.

---

## What this does not cover

- **Endpoint security on member machines.** If a member's computer is compromised, the mesh layer cannot protect their household. Members are responsible for their own endpoint security.
- **Physical security.** Someone with physical access to a peer machine can extract its WireGuard private key. Full-disk encryption is the user's protection.
- **DDoS against coordinator infrastructure.** Standard infrastructure hardening applies and is outside this doc.
- **Model-level safety.** What the models emit when prompted is out of scope. Potluck shows model outputs without filtering.

---

## Cross-references

- `docs/phase-g/mesh-design.md`, section "Privacy posture": the privacy invariants and what each component sees.
- `docs/phase-g/local-ipc.md`: the local control interface of the mesh sidecar.
- `docs/security/peer-quality.md`: quality scoring and feedback.
- `docs/security/release-key.md`: contributor release attestation.
- `services/coordinator/tests/test_privacy_invariants.py`: the tests that pin the structural privacy claims.

To report a vulnerability, write to rob@trypotluck.ai. See [SECURITY.md](../../SECURITY.md).
