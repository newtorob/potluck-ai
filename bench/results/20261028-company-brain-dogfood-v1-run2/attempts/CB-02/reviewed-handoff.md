# Investigation handoff

## Issue
Investigate the security boundary of opt\-in MCP evidence access: what access does its configured scope grant, and does it prevent an agent from sending source text to a model provider?

Status: reviewed
Investigation: 5a3e1665\-922f\-4b37\-b16a\-ea3fa3612e05
Model: qwen\-2\.5\-3b\-instruct\-q4
Model SHA256: 9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94
Created: 2026\-10\-29T02:37:02\.941006\+00:00

Workspace: potluck\-dogfood\-v1
Collections: 52e912f9\-4660\-41a4\-93ee\-98c4e15a48f5
Template: investigation\-v3
Retrieval: unicode\-word\-overlap\-v1
Generation settings: \{'temperature': 0, 'max\_tokens': 1600, 'timeout\_seconds': 240\}

## Facts
- The owner sets a fixed workspace and collection scope at MCP startup; neither evidence tool accepts scope arguments, and changing configuration requires restarting the process\. (source:cfbb969a\-e843\-5644\-883a\-26828a0b476f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)
- The agent receives read access to the entire selected scope\. The scope does not prevent it from forwarding retrieved text to its configured model provider\. (source:cfbb969a\-e843\-5644\-883a\-26828a0b476f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)
- The local trust boundary is private stdio or the existing bearer\-token gate for optional loopback TCP; this does not supply authenticated per\-user organizational access enforcement\. (source:cfbb969a\-e843\-5644\-883a\-26828a0b476f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)
- Both memory privacy switches must be enabled and unavailable settings fail closed\. The evidence allowlist does not govern separate personal\-memory tools\. (source:cfbb969a\-e843\-5644\-883a\-26828a0b476f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)

## Prior fixes
- None recorded

## Hypotheses
- None recorded

## Recommended checks
- Inspect the actual agent and provider configuration before granting access to private sources, and test any required organizational authorization separately\. (source:cfbb969a\-e843\-5644\-883a\-26828a0b476f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0)

## Missing evidence
- The retrieved documentation does not establish the configured provider or whether source text was transmitted in any particular execution\.

## Evidence provenance
- source:cfbb969a\-e843\-5644\-883a\-26828a0b476f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · opt\-in\-mcp\-access
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk cfbb969a\-e843\-5644\-883a\-26828a0b476f
- source:15256c07\-5d25\-52b6\-9fa9\-924f48d0857f:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · saved\-investigations\-in\-the\-desktop\-app\-1
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 15256c07\-5d25\-52b6\-9fa9\-924f48d0857f
- source:881a5d2b\-68e0\-5227\-88f9\-53908ddce790:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · verification\-and\-remaining\-work
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 881a5d2b\-68e0\-5227\-88f9\-53908ddce790
- source:c23428de\-8aa9\-5cf9\-ace1\-33021243a6f5:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · sources\-and\-authority\-1
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk c23428de\-8aa9\-5cf9\-ace1\-33021243a6f5
- source:1bc34934\-5ea0\-57dc\-a11e\-0875609e7b28:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · review\-outcomes\-and\-reuse\-1
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 1bc34934\-5ea0\-57dc\-a11e\-0875609e7b28
- source:9ec6be25\-6142\-555b\-9a54\-65c4fe3620ce:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · sources\-and\-authority\-2
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 9ec6be25\-6142\-555b\-9a54\-65c4fe3620ce
- source:afd96bc1\-6f18\-5f4b\-9ae8\-8bc3102444e6:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · saved\-investigations\-in\-the\-desktop\-app\-2
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk afd96bc1\-6f18\-5f4b\-9ae8\-8bc3102444e6
- source:d42f5ce3\-2cdb\-5fa3\-97bf\-78fb10b81b68:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · each\-result\-has\-a\-citation\_id\-resolve\-it\-again\-before\-using\-a\-saved\-citation
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk d42f5ce3\-2cdb\-5fa3\-97bf\-78fb10b81b68
- source:94323ce2\-4249\-5059\-9240\-e78121c4aeeb:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · local\-company\-brain\-evidence\-and\-review
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk 94323ce2\-4249\-5059\-9240\-e78121c4aeeb
- source:e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d:9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · COMPANY\_BRAIN\.md · freshness\-and\-removal
  - COMPANY\_BRAIN\.md · revision 9dfc2a42\-81e9\-591f\-8d1e\-e0f19541dde0 · chunk e566b3fd\-bbc2\-5263\-95b5\-4197dee0b49d

## Review
correct by Codex internal dogfood \(AI review\): Primary reviewed the independent Terra assessment against the exact cited source\. Corrected factual attribution, removed repeated hypotheses, added omitted boundaries\. This is AI\-assisted internal review, not human or customer acceptance\.
The sections above use the reviewed correction. The original draft is retained in Potluck.

## Actual outcome
inconclusive by Codex internal dogfood \(AI review\): inconclusive: documentation\-based test only; no real agent/provider forwarding or enforcement was tested
