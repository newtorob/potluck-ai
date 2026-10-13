# Repository edit evaluation v1

Three small, synthetic, two-file Python repairs exercise the **real CLI agent
core**, filesystem tools, pinned gateway requests, and automatic verify loop:

- Booking: half-open interval semantics and a caller accepting iterators.
- Retry: exhaustion, backoff, Retry-After, and HTTP status eligibility.
- Contacts: quoted CSV and normalized deduplication with stable ordering.

The runner imports the built `runTask`; it does not exercise the terminal UI,
argument parser, or interactive approval prompts. It grants edits only to the
two designated source files. Shell tools are denied. The model sees the task,
repository map, original sources, visible tests, and actual verification results.
Reference implementations and additional oracle tests stay outside its workspace.

## Run

Build the CLI, then use **Linux with bubblewrap and util-linux (`prlimit`)**:

```sh
npm run build
node scripts/eval-repo-edits.mjs --self-test --out /tmp/potluck-eval-checks
node scripts/eval-repo-edits.mjs \
  --lock /path/to/pinned-project/potluck.lock \
  --out /path/to/private-results
```

Use `POTLUCK_DATA_DIR` and `POTLUCK_GATEWAY_URL` for a separate test runtime.
The gateway must be on loopback and enabled. The supplied project lock must
match its loaded model. Configure the runtime with **8,192 context tokens**;
for a managed split, pass `--context 8192` to `split start`. Runtime provisioning,
context verification, and cleanup are the operator's responsibility; the runner
does not change services or download models. Record runtime versions, placement,
network transport, load time, and hardware alongside its report.

`--case booking|retry|contacts` selects one fixture for diagnosis. Compare both
models with identical fixtures and limits, and disclose any excluded attempts.

## Bounds and grading

Each trial gets a fresh repository, at most 16 agent turns, a 15-minute wall
deadline, a 16,000-character history budget, temperature 0.2, and a 4,096-token
output cap per turn. Character budgeting is approximate; context errors are
failures rather than a reason to silently change model, context, or scope.

Verification uses Python's standard library in a fresh bubblewrap namespace:
no network, no home directory or credentials, read-only fixture and interpreter
mounts, 10 seconds CPU, 15 seconds wall, 768 MiB address space, and bounded output.
There is **no unsandboxed fallback**. The fixed visible test command runs after
successful edits; independent oracle checks run only after the agent finishes.

A pass requires changed source files, unchanged visible tests, a successful
automatic verify, passing visible and oracle checks, and no stream error,
deadline, or loop-limit notice. Final prose never counts as success. Self-test
proves that the original code fails assertions and reference repairs pass both
sets of checks. These reference repairs are never supplied to the model.

Reports contain fixture hashes, exact model identity, policy, timing, tool and
verify events, conversation history, and final check output. They contain
synthetic code but may include local paths; keep raw reports private by default.

This is a small acceptance suite, not evidence of a general coding-quality
advantage. One trial per case does not measure variance. Different model sizes,
quantizations, backends, and placement confound model-size-only comparisons.
Freeze fixtures before comparing models; revise the version when changing the
tasks or grading policy after observing model behavior.
