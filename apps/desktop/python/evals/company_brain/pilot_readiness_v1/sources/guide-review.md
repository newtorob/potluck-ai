## Review, outcomes and reuse

1. Explicitly accept, correct or reject a draft, with a reviewer and reason.
   A correction preserves the original draft and can change text, citations and
   excerpts. Facts and prior fixes need exact excerpts even when correcting a legacy
   draft. Historical combined missing evidence remains in the original draft; the
   reviewer supplies new unverified questions separately. Retrieval notices remain
   immutable. The review is one-shot; it is not a collaborative editor.
2. Record the actual outcome separately as resolved, unresolved or inconclusive,
   with notes. Accepting a brief does not resolve the underlying issue. An outcome
   is also one-shot; create a new investigation for a later observation.
3. Export a Markdown handoff, or after review and outcome explicitly download an
   evaluation-case proposal. The proposal contains references and review/outcome
   categories rather than source text or an automatically accepted test answer.
4. An accepted/corrected investigation with an outcome can propose knowledge using
   citations from the reviewed brief. The claim remains **proposed**; approval
   still uses the separate owner review CLI. This does not train a model.

The HTTP endpoints are under `/company-brain` and use the sidecar's existing
loopback/token middleware. They are unavailable through peer/remote routes.
Workspace strings and reviewer names are organizational labels, not authenticated
company identities. Both memory privacy switches are required. Exports already
