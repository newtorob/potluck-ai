# Peer quality scoring

## Why this exists

A peer can return technically-valid but qualitatively-bad completions: wrong
model or quantization, broken sampling, adversarial output. Reputation (the
verification canary loop) catches fabrication on specific probe prompts.
Quality scoring catches the broader degradation pattern: a peer that
consistently produces bad outputs for real inference jobs.

The two systems are complementary. Reputation is a coordinator-driven probe
signal. Quality is an aggregate of observed signals from real dispatch.

## Score formula

For each (peer, model_slug) pair, the coordinator maintains a log of quality
observations in the `peer_quality_scores` table. The current quality score is
a recency-weighted mean over a sliding window:

```
quality_score = sum(w_i * score_i) / sum(w_i)

where:
  w_i        = sample_size_i * 2^(-age_hours_i / QUALITY_HALF_LIFE_HOURS)
  age_hours_i = hours elapsed since observed_at
```

**Constants (all in `services/peer_quality.py`):**

| Constant | Value | Meaning |
|---|---|---|
| `QUALITY_WINDOW_DAYS` | 7 | Observations older than 7 days are excluded |
| `QUALITY_HALF_LIFE_HOURS` | 24 | An observation 24 h old has half the weight of a fresh one |
| `QUALITY_FAIL_THRESHOLD` | 0.3 | Score below this = failure observation |
| `QUALITY_CONSECUTIVE_FAIL_THRESHOLD` | 3 | Hard-exclude after this many consecutive trailing failures |
| `QUALITY_NEUTRAL` | 1.0 | Default score when no observations exist in the window |

At 4 days (96 h), an observation has weight `2^(-96/24) = 2^(-4) = 1/16`
relative to a fresh one.

## Neutral default

A peer with no observations in the current window receives `quality_score =
1.0` (neutral). New peers are not penalised. The open-pool reputation gate
already protects strangers from unverified peers; quality is an additional
signal that kicks in once observations accumulate.

## Dispatch integration

Quality multiplies into the existing peer selection score:

```
score = compute_score / (1 + current_jobs) * (0.5 + reputation) * quality_score
```

All three factors are retrieved in the same batched queries that run for every
dispatch (`_score_peers` in `services/peer_registry.py`). A peer with
`quality_score = 0.5` has its composite score halved relative to a peer with
the same compute/load/reputation profile.

## Hard-exclude policy

A peer is **removed from dispatch eligibility** (not merely deprioritised) when
its most recent N consecutive observations are each below
`QUALITY_FAIL_THRESHOLD` (0.3), where N = `QUALITY_CONSECUTIVE_FAIL_THRESHOLD`
(default 3).

Rules:
- **Never** triggered by a single bad signal. It requires N = 3 consecutive failures.
- Applies to **all routing scopes** (My Machines, circle, open pool). A peer
  consistently failing quality checks should not serve anyone.
- The exclusion lifts as soon as one passing observation arrives (score >= 0.3),
  resetting the consecutive count.
- The check is a single window-function query over the eligible candidate set,
  with no per-peer round-trips.

## Signal types

| `signal_type` | Source | Notes |
|---|---|---|
| `user_feedback` | `POST /jobs/{job_id}/feedback` | Binary: up=1.0, down=0.0 |
| `determinism` | Probe scheduler (planned) | Re-run same prompt, compare output |
| `coherence` | Shadow eval (planned) | LM-judge coherence on sampled outputs |
| `latency` | Job completion (planned) | Normalised tokens/s vs. peer's advertised TPS |

Only `user_feedback` is wired. The probe scheduler, shadow-eval probes and
latency signal are planned (see the last section).

## Feedback endpoint

```
POST /jobs/{job_id}/feedback
X-Potluck-Coordinator-Token: <token>
Content-Type: application/json

{"rating": "up" | "down"}
```

The `X-Potluck-Coordinator-Token` header is required when the coordinator is
configured with a shared token (`POTLUCK_COORDINATOR_TOKEN`).

Response: `{"recorded": true}` (200) or 404 if the job is unknown or has no
assigned peer.

**Privacy:** The endpoint reads only `job.peer_id` and `job.model_slug` from
the jobs row. It records only the binary rating (mapped to 0.0 / 1.0) plus
those two routing-metadata fields. Prompt and completion content are never
read, stored, or forwarded. `signal_type = "user_feedback"`, `sample_size = 1`.

## Privacy stance

`peer_quality_scores` rows contain:
- `peer_id`: which peer served the job (routing metadata, already known to the coordinator)
- `model_slug`: which model was used (routing metadata)
- `signal_type`: category of signal
- `score`: scalar 0..1
- `sample_size`: number of inferences this observation covers
- `observed_at`: timestamp

No prompt content, no completion content, no requester identity. The
aggregate quality score is an internal coordinator signal and is not
forwarded to the peer, to the requester, or to any public endpoint. The
`/network` transparency view does not carry peer-bound quality scores
(that would link a peer to its quality history in a requester-visible way).

## Planned (not built)

- **Probe scheduler**: periodic coordinator-initiated probes that call
  `record_quality_signal` with `signal_type='determinism'` or `'coherence'`.
  Requires a job-dispatch-to-peer path as the signal source.
- **Commit-reveal adversarial probes**: the peer commits to a hash of its
  response before revealing it, preventing post-hoc adjustment when it
  detects a canary. Builds on the existing escrow / verification loop.
- **Frontend quality badges + thumbs-down UI**: the desktop UI needs a
  post-response "was this helpful?" widget that calls the feedback endpoint.
- **Bench-prompt ground truth**: a determinism probe needs a ground-truth
  corpus against which to compare; generating and curating that is separate
  work.
- **Per-model quality leaderboard**: aggregate quality scores per model_slug
  across the network (useful for capacity planning and operator transparency),
  but requires careful privacy review before exposing in any public view.
