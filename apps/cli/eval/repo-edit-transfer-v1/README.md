# Repository edit transfer checks v1

Two additional two-file repairs, authored and frozen on September 29 before
changing the agent prompt/tools. These are small transfer checks, not an industry
benchmark or a statistically representative quality score. Reference repairs
and independent checks remain outside the agent workspace.

Settings tests recursive configuration precedence, independent copies and type
validation. Routing tests normalization, segment boundaries, longest-prefix
selection, stable ties and target identity. Task text states every independently
checked behavior. Visible checks deliberately cover only part of each contract.

Use the same runner, model pin and limits as `repo-edit-v1`:

    node scripts/eval-repo-edits.mjs --suite repo-edit-transfer-v1 --self-test --out /tmp/transfer-self-test
    node scripts/eval-repo-edits.mjs --suite repo-edit-transfer-v1 --lock /path/potluck.lock --environment /path/environment.json --trials 3 --out /path/results

The default runner still selects the unchanged original suite. Both baseline
and candidate must use the same runner and frozen fixture bytes. Never modify
fixtures after looking at candidate results; add a new version instead.

The shared runner uses harness version 3 and report schema 2. It records all repetitions,
including failures and unrun attempts; checks exact model admission and the
loaded 8,192-token context; and separates operator-declared runtime conditions
from observed metadata. Independent checks are never used for repair feedback
or candidate selection. See the [runner documentation](../repo-edit-v1/README.md)
for environment fields, grading, timing definitions, offline fixture validation
and limitations. The five task/project/reference/oracle trees remain unchanged;
the strengthened harness does not itself demonstrate better model performance.
