# Fixture review after freeze, before candidate results

On 2026-10-30, the primary first read the six-case fixture after the candidate contract was frozen and the v11 run started. A follow-up Terra author review, without reading outputs, confirmed these limitations. Frozen sources, plans, rubric and denominators remain unchanged.

- Quartz asks for a difference in days, a percentage change, and measurement meaning. Its supplied plan contains only one `difference` operation. The existing contract returns one calculation per operation and does not explicitly authorize the second percentage calculation. A missing percentage cannot cleanly be attributed to a candidate regression. Keep the case in the declared six-case score and annotate the plan limitation; do not silently score a reduced question.
- The six scoped source totals are 1,680, 1,575, 1,478, 1,242, 1,480 and 1,263 bytes. Four are below the intended approximate 1,500–3,000-character scope, two only narrowly. These are small synthetic cases.
- The requested list/blank-line-before-list formatting was not included by the fixture author. The fresh comparison does not test that formatting repair; the deterministic regression and earlier consumed packaging request do.
- No fresh comparison result should be presented as whole-vault, long-document or company pilot qualification. Real Potluck source diagnostics remain a separate, failed gate.
