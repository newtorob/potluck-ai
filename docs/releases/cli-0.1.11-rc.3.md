# Potluck CLI 0.1.11-rc.3

This release candidate contains five fixes for interrupted coding tasks, file
changes made alongside the agent, and misleading completion reports.

## Changes

1. Cancelling a task stops its running shell command or configured verification
   check. On macOS/Linux, the CLI terminates the command's process group and
   escalates for children that ignore termination. Cancelling an approval prompt
   also prevents its file write. Previously completed file changes are kept.
2. File edits recheck the current target and contents after approval. Changes
   made by an editor or formatter while the approval prompt is open are preserved;
   the agent must inspect the current file and present a fresh diff.
3. Whole-file replacement requires the same contents the agent fully read.
   An external change invalidates that permission. A targeted edit also requires
   a fresh full read before a later whole-file replacement.
4. A failed file change cannot be followed by a successful completion claim
   until an actual later file edit succeeds. Passing checks alone cannot clear
   that failure. Declining an action stops the task incomplete and allows a
   follow-up instruction; one-shot mode exits nonzero.
5. Successful diagnostic shell commands no longer erase the record of repeated
   failed edits or unchanged reads. The agent stops those loops within its
   existing recovery limits.

## Downloads

- macOS 15 or newer, Apple Silicon: `potluck-cli-0.1.11-rc.3-darwin-arm64.tar.gz`
- Linux x86-64, glibc 2.35 or newer: `potluck-cli-0.1.11-rc.3-linux-x64.tar.gz`

Node.js 20 or newer is required. The archives include the unchanged Python
runtime from desktop 0.1.12-rc.2; models are downloaded separately. CLI source
and bundled-runtime provenance are recorded separately in release validation.

This is a CLI prerelease. Install from its archive and verify its SHA-256 against
`SHA256SUMS` or the adjacent `.sha256` file. Homebrew continues to provide the
stable CLI. The desktop app's release version is unchanged.

## Scope and limits

These changes improve specific failure handling; they do not establish a general
coding-quality or speed advantage. Frozen task evaluations are separate from
unit, integration and package startup checks. The evaluation harness also retains
completion evidence for verbose checks using a bounded output allowance shared
by both comparison versions.

File revalidation protects the approval wait window but is not an atomic
filesystem transaction. Process-group cancellation does not reach processes that
deliberately detach into a new session. Windows downloads are not included.
