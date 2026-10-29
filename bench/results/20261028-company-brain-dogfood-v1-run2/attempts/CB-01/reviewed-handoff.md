# Investigation handoff

## Issue
Investigate what happens to reviewed claims when a source document changes, and whether a failed scan can leave the prior snapshot available as current evidence\.

Status: reviewed
Investigation: 9945b153\-2f4f\-4007\-84b0\-6c2f0b62d247
Model: qwen\-2\.5\-3b\-instruct\-q4
Model SHA256: 9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94
Created: 2026\-10\-29T02:36:02\.724427\+00:00

Workspace: potluck\-dogfood\-v1
Collections: 52e912f9\-4660\-41a4\-93ee\-98c4e15a48f5
Template: investigation\-v3
Retrieval: unicode\-word\-overlap\-v1
Generation settings: \{'temperature': 0, 'max\_tokens': 1600, 'timeout\_seconds': 240\}

## Facts
- A changed or removed document invalidates and scrubs all claims bound to it, including proposed and superseded payloads, in the same scan transaction\. (source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)
- A failed scan rolls back changes, retains the previous snapshot, and blocks current retrieval until a successful rescan\. (source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)
- Disk changes become visible only after an explicit scan; a recent snapshot does not establish that disk changes were indexed\. (source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)
- Scrubbing removes active logical content; it does not guarantee forensic erasure from SQLite WAL, filesystem backups, or previously returned responses\. (source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)

## Prior fixes
- None recorded

## Hypotheses
- None recorded

## Recommended checks
- In a disposable test, change a bound source and rescan, then separately induce a scan failure and check retrieval and snapshot behavior\. (source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)

## Missing evidence
- The retrieved documentation does not establish that these operations were executed or what happened in a specific environment\.

## Evidence provenance
- source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · freshness\-and\-removal
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d
- source:7b47a64a\-92e8\-5fd2\-94d2\-6db41f3f8976:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · review\-outcomes\-and\-reuse\-2
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 7b47a64a\-92e8\-5fd2\-94d2\-6db41f3f8976
- source:c23428de\-8aa9\-5cf9\-ace1\-33021243a6f5:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · sources\-and\-authority\-1
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk c23428de\-8aa9\-5cf9\-ace1\-33021243a6f5
- source:9ec6be25\-6142\-555b\-9a54\-65c4fe3620ce:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · sources\-and\-authority\-2
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 9ec6be25\-6142\-555b\-9a54\-65c4fe3620ce
- source:15256c07\-5d25\-52b6\-9fa9\-924f48d0857f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · saved\-investigations\-in\-the\-desktop\-app\-1
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 15256c07\-5d25\-52b6\-9fa9\-924f48d0857f
- source:d42f5ce3\-2cdb\-5fa3\-97bf\-78fb10b81b68:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · each\-result\-has\-a\-citation\_id\-resolve\-it\-again\-before\-using\-a\-saved\-citation
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk d42f5ce3\-2cdb\-5fa3\-97bf\-78fb10b81b68
- source:881a5d2b\-68e0\-5227\-88f9\-53908ddce790:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · verification\-and\-remaining\-work
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 881a5d2b\-68e0\-5227\-88f9\-53908ddce790

## Review
correct by Codex internal dogfood \(AI review\): Primary reviewed the independent Terra assessment against the exact cited source\. Corrected factual attribution, removed repeated hypotheses, added omitted boundaries\. This is AI\-assisted internal review, not human or customer acceptance\.
The sections above use the reviewed correction. The original draft is retained in Potluck.

## Actual outcome
inconclusive by Codex internal dogfood \(AI review\): inconclusive: documentation\-based test only; no actual source change or failed scan was performed
