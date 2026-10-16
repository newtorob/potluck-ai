# Repository edit evaluation v1

Three frozen, synthetic, two-file Python repairs exercise the **real CLI agent
core**, filesystem tools, pinned gateway requests, and automatic verification:

- Booking: half-open interval semantics and a caller accepting iterators.
- Retry: exhaustion, backoff, Retry-After, and HTTP status eligibility.
- Contacts: quoted CSV and normalized deduplication with stable ordering.

The runner imports built `runTask`; it does not exercise terminal UI, argument
parsing, interactive approvals, or automatic `AGENTS.md` loading. These fixtures
have no project instruction files. Edits are allowed only to the two designated
source files; shell tools are denied. Reference implementations and independent
tests stay outside the agent workspace. Independent results are never sent to
the model or used to choose a retry or candidate.

## Run

Build the CLI, then use **Linux with bubblewrap and util-linux (`prlimit`)**:

```sh
npm run build
node scripts/eval-repo-edits.mjs --self-test --out /tmp/potluck-eval-checks
node scripts/eval-repo-edits.mjs \
  --lock /path/to/pinned-project/potluck.lock \
  --environment /path/to/environment.json \
  --trials 3 --out /path/to/private-results
```

Use `POTLUCK_DATA_DIR` and `POTLUCK_GATEWAY_URL` for a separate test runtime.
The gateway must be enabled and on loopback. The supplied lock must match its
loaded model. Configure **8,192 loaded context tokens**; for a managed split,
pass `--context 8192` to `split start`. Before each trial, the runner asks the
local gateway to check the exact artifact pin and report its loaded context.
Unknown or mismatched context stops the run. This is a snapshot, not a runtime
reservation; later reloads can still make a request fail. Inference itself
continues to enforce the exact model pin and local scope.

Runtime provisioning and cleanup remain the operator's responsibility. The
runner does not change services, download models or broaden routing. A transport
or execution error stops further inference in case a managed split was affected.
Remaining planned trials are retained as `not_run` with the reason.

`--case booking|retry|contacts` selects one fixture. `--trials` accepts 1–20;
the default is three repetitions per case, interleaved by case, with a fresh
workspace and conversation each time. Self-test defaults to one repetition and
uses reference repairs without inference. There is no selection of the best
repetition: all planned attempts remain in the denominator.

## Reproducible conditions

Supply an environment JSON with the actual conditions, for example:

```json
{
  "schemaVersion": 1,
  "runtimeVersion": "exact Potluck runtime version or revision",
  "backend": { "name": "llama.cpp", "version": "exact wrapper and native build revisions" },
  "hardware": ["CPU model and RAM; GPU model and VRAM for each participating machine"],
  "placement": "single machine, or exact layer placement for a split",
  "transport": "none, or link type and conditions for a split"
}
```

These backend/version/hardware/placement fields are **operator-reported**, not
independently verified by the evaluator. The report labels their provenance.
Host OS, CPU model, RAM and Node version are collected locally; they describe
the runner's host, which may differ from other machines in a managed split.
Python, bubblewrap and prlimit versions are also recorded from the checker host.
Loaded context and model-pin admission are gateway evidence at trial start.
The report includes the complete model lock, fixture hashes, runner hashes,
compiled CLI JavaScript hashes, Git revision and whether relevant source/script
files were dirty. Compiled hashes identify the code actually imported even in a
dirty checkout. No credentials are included in environment fields.

Task, project, reference, oracle and manifest bytes contribute to the fixture
fingerprint; README prose does not. Compare runs only with identical frozen
fixture bytes, harness version, model artifact, and disclosed conditions.
Harness version **3** (report schema 2) captures up to 2 MiB per output stream
from each checker so a broken fixture's assertion output does not discard its
completion record. An output-limit exit remains a failed check, even if a
completion record was already printed. Reports retain only the last 12,000
characters. Run both candidates with the same harness; do not pool scores from
different harness versions without rerunning them.

## Bounds and grading

Each trial allows at most 16 agent turns, four consecutive failed automatic
checks, a 15-minute agent wall deadline and a 16,000-character upper bound.
The agent additionally budgets against the confirmed loaded window, reserving
output and template space with a conservative UTF-8 estimate. At 8,192 context
tokens the effective output allowance is **2,048**, recorded in policy; it is
not the old runner's hardcoded 4,096. Temperature is 0.2.

Checks use Python's standard library in a fresh bubblewrap namespace: no
network, home or credentials; read-only fixture and interpreter mounts;
10 seconds CPU, 15 seconds wall, 768 MiB address space and bounded output.
There is **no unsandboxed runner fallback**. The visible checks run after edits.
Independent checks run only after the agent ends, including after a caught
agent error. A successful process exit alone does not pass: a completion record
must confirm tests executed with no failures, errors or skipped tests. This
rejects accidental early `sys.exit(0)` or `os._exit(0)` during imports.

A pass requires the agent's explicit `finished` outcome, successful automatic
verification, passing final visible and independent checks, both required source
files changed and nonempty, and no changes to protected files or added files.
Informational notices such as context compaction do not make a correct result
fail. Incomplete outcomes, transport errors, deadlines and destructive changes
remain failures; final prose never establishes correctness.

Each report records verification attempts/retries, failed checks, tool failures,
denied actions, first token, and first visible verification success. The
**time to verified result** begins at agent start and includes final visible and
independent grading. Failures and unrun trials have `null`, not a fabricated time.
Self-tests also have `null` model timings. Full trial duration separately includes
workspace preparation and baseline checks. Results retain edited workspaces,
history and check output; `events.jsonl` is appended during execution so an
interrupted process leaves its tool/verification evidence. A running record in
an interrupted report is incomplete, never a pass.

## Fixture and harness validation

`pnpm exec vitest run test/eval-harness.test.ts` runs offline grading/metadata
regressions and checks all five frozen baseline/reference pairs with `python3`.
It executes only repository-owned fixture/reference code, never model-generated
repairs. This development check can run on macOS and is **not** the Linux
bubblewrap self-test or evidence that Linux sandbox execution works there.

Both validation paths require original code to fail assertions, and references
to pass visible and independent checks. The oracles are isolated from agent
tools and feedback, but are not a secure grading system against deliberately
hostile Python that tampers with unittest internals.

These small synthetic cases do not establish a general coding-quality advantage.
Repetitions expose some variance; they do not make the tasks representative.
The fixtures are public and may be familiar to a model. Changing model sizes,
quantization, backend or placement introduces confounds. Freeze tasks and grading
before comparing candidates; version changes after seeing results. Keep raw
reports private by default: synthetic traces may include local paths. A published
benchmark claim still needs its raw run in `bench/results/` and full conditions.
