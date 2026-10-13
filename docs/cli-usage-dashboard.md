# Terminal usage dashboard

`potluck usage` shows request, token and latency totals recorded on this computer.
Both the CLI and the running runtime must include the dashboard.

```sh
potluck usage                    # live dashboard in a terminal
potluck usage models             # compare model performance
potluck usage requests           # recent request history
potluck usage machines           # runtime, owned household and split status
potluck usage --window 30 --direction served
potluck usage --once              # one readable snapshot
potluck usage --json              # one metadata document
potluck usage --json --watch --interval 5000
potluck usage --theme light
```

From a source checkout, build with `cd apps/cli && npm run build`, then use
`node dist/index.js usage`. Start the matching source runtime in an isolated data
directory or restart your normal runtime after upgrading. An older running
runtime shows an unavailable screen with upgrade guidance.

## Views and controls

- **Overview:** request and token totals, end-to-end output rate, average first
  token latency, p95 total latency, activity chart, recent requests and local status.
  The routing panel shows counts and shares for this machine, your other machines
  and the open pool; trusted-circle and unclassified records appear when present.
  These are recorded routes, not proof that a route is currently available.
- **Models:** the top 20 model IDs by requests in the selected window, including
  tokens, end-to-end rate, first token latency, success rate and rate sample count.
- **Requests:** the latest 100 completed records, newest first, with model, route,
  tokens, latency and success/failure. Arrow keys scroll; page keys move ten rows.
- **Machines:** local model, context capacity, free system RAM (bar shows free
  share), last benchmark, aggregate owned-household availability and split status.
  This view probes only the user's owned machines, with a bounded timeout.

`1`–`4` or Tab switch views; `w` cycles 1/7/30/90 days; `d` switches consumed and
served records; Space pauses automatic polling; `r` refreshes (also while paused);
`t` changes theme; `?` opens help; `q`, Escape or Ctrl-C quit. On POSIX, Ctrl-Z
restores terminal state before suspending; continuing reacquires it. Polls run
sequentially, every two seconds after the previous sample completes. Resizing
redraws the screen. A connection failure preserves the last snapshot and labels
it stale until a successful refresh.

The terminal uses Potluck's Hearth colors, a dependency-free cell-aware renderer,
Unicode charts, and an alternate screen. Colors adapt to truecolor, 256-color
and 16-color terminals (including explicit screen/tmux declarations). It restores keyboard mode, cursor and
stdin flow on exit, cancellation and exceptions. `NO_COLOR` disables color;
`TERM=dumb` uses ASCII snapshots. Piped output is a single uncolored snapshot by
default. `--watch` explicitly enables repeated snapshots or NDJSON. Exit codes:
0 for success/normal watch exit, 1 for an unavailable single snapshot, 2 for bad
arguments.

Roomier overview terminals use three-row headline digits. Latency gets separate
cards at 120 columns; smaller layouts use a clearly labeled latency line.
Plain/ASCII output keeps ordinary, copyable numbers. Below 64 columns or 24 rows,
the view shows a compact summary and a resize hint.

## What the numbers mean

Everything is recorded **on this computer**, not collected into a fleet-wide
billing ledger. Consumed rows represent requests made here, including routed
requests. Served rows represent work this runtime did for others. Selecting
Machines does not change the usage ledger's scope.

All aggregations use the same rolling window and a single SQLite read transaction.
Activity buckets are UTC hours for 1 day and UTC days for longer windows. The
first and last buckets are partial. The graph labels its zero and peak request
counts. The orange bar is the current UTC interval, marked with an asterisk for
the partial bucket. When adjacent buckets are merged at narrow widths, the graph
labels the resulting size and the orange bar as the last bin (which includes the
current interval). Color is supplemented by text in monochrome output.

- Totals include recorded successful and failed requests. Token counts are only
  what the request path reported; they are not tokenizer estimates.
- **E2E tok/s** is total reported output divided by total wall time for successful
  requests with positive output and duration. It includes prefill and routing
  overhead. It is not live decode speed or an average of per-request rates.
- First-token and duration averages, and nearest-rank p95 duration, use successful
  samples with recorded nonnegative measurements. Missing samples remain null
  in JSON and `--` in the terminal.
- A runtime benchmark is labeled **Last benchmark**, separately from usage rates.
- Context capacity is the configured limit, not live occupancy. RAM is system
  memory, not GPU VRAM. GPU utilization, watts, live decode speed and cache hits
  are not measured by this screen.

`GET /usage/dashboard?window=7&direction=consumed&household=false` is an authenticated
local read endpoint under the existing sidecar access boundary. It returns an
explicit metadata allowlist: no prompts, completions, error messages, credentials,
paths, session IDs or peer identities. The CLI also bounds/allowlists the response
and neutralizes terminal control characters in model names. Viewing usage does
not start inference, download models, change routing or enable contribution.

## Rendering previews from sample data

`apps/cli/scripts/preview-usage-dashboard.mjs` renders the terminal UI to ANSI and
HTML files from synthetic test data. It never connects to a runtime. After
building the CLI, run `node scripts/preview-usage-dashboard.mjs <output-directory>`
from `apps/cli`. Each HTML file carries a **SAMPLE DATA - VISUAL PREVIEW** banner.
The previews are layout checks and say nothing about inference performance.
