# Local-agent scalar vocabulary correction — November 12, 2026

The opt-in `test-matrix-v6` role constrains hypothetical input names and values
using a small caller-declared vocabulary. Generation uses the existing native
provider's supported string enums; host validation requires the same tokens and
retains existing reference, word/byte, duplicate, unresolved and instruction-echo
guards. Full quoted examples belong in question prose. V5 messages, schemas and
validation remain unchanged. No reply repair, coercion or generation retry exists.

The first pattern-based attempt, V205, failed provider validation before any model
generation: three HTTP attempts, zero native calls. That failure remains archived.
V206 uses admitted enums without changing the runtime/gateway schema guards.
The original V204 whitespace-bearing reply remains rejected under both versions;
its failure and all earlier Company Brain scores remain unchanged and unpooled.

After the final implementation freeze, primary authored two fresh synthetic
assignments. They and one consumed setup assignment ran through actual owned-local
Potluck CLI agents, serially, one call each. All three original packets passed
strict formatting/reference validation. Primary unblinded source-aware review
found four of six criteria fully covered: consumed setup 1/2, fresh bundle 2/2,
fresh literal request 1/2. Only one whole packet covered both criteria. The setup
proposal omits a concrete quoted question and confirmation; the literal-request
proposal preserves the Unicode example and confirmation but does not explicitly
test one-request/draft preservation. Four proposals are eligible for further use;
no product tests were implemented or executed from them and no new findings were
adopted. Format success is not correctness.

Local checks passed: 1,382 CLI tests, 553 frontend and 44 landing tests, typechecks,
privacy lint and a local landing build. Existing backend checks were not rerun for
this CLI-only change. Native inputs were 722/646/630 tokens, outputs 256/287/255,
under 1100 input/700 output caps. CLI wall times were 53.308/57.715/51.508 seconds;
script 190.282 seconds, watchdog 198.195 seconds including nested load/profile 27.483 seconds.
These timings are not additive and do not measure human effort. There were no
answer calls, retries, field repairs or tool executions.

The pinned owned Qwen3.8-27B IQ4_XS runtime used 8192 context tokens, 8 CPU threads and 10 GPU layers,
seed 7 and the existing non-thinking profile. Configured prompt-cache allocation
was 0 MiB, but observed native cached-prefix counts were 0/206/206; these were not all
cold calls. Effective schemas/sampler settings and source hashes were audited.
Native replies equal SSE content exactly; the existing harness adds a single
turn-end newline to its separate trace. An initial audit assumption missed this
suffix; the failing script/log and correction are preserved without artifact edits.

Watchdog exit 0/no timeout; independent checks verify all four owned processes and
watchdog gone, package hashes intact and model/bridge loopback ports closed. No
ordinary gateway, model or defaults changed. No cloud CI, release or social post.

Limits: the caller narrows scenario tokens; vocabulary membership cannot verify
semantic pairings or expectations. Primary authored and reviewed the new synthetic
controls, so this is neither an independent holdout nor new-company qualification.
A stale helper-brain provenance label about retention excerpts was also retained
as an experiment-template defect; actual assigned sources were the frozen synthetic
files. Future jobs must name their actual inputs. No Rob acceptance or usability
measurement is claimed. Keep V6 opt-in and separately qualify missing one-draft
and confirmation coverage before claiming broader helper reliability.

[summary.json](summary.json) binds the private owner-retained evidence archive.
Raw replies, source files, databases and full execution packets are excluded from
this public summary. See [the role contract](../../../docs/potluck-agent-harness.md).
