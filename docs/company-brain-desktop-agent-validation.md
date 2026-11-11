# Desktop question controls and applied local-agent checks

This opt-in build adds editable question parts to the reviewed Company Brain
pilot and a separate scalar test-design role for the Potluck CLI. Existing
answering defaults, Python/V32 implementation and older agent contracts remain
unchanged. It is ready for a bounded hands-on review, not a production-readiness
or independent company-scale qualification claim.

## Result

| Check | Result |
| --- | --- |
| Desktop frontend tests | 546 passed; typecheck passed |
| CLI build and tests | 1,355 passed |
| Landing tests and local production build | 44 passed; types/build passed |
| Privacy checks | Passed |
| Real browser controls | Passed edit, confirmation invalidation, error, recovery and 800 px layout; mocked API, no model-quality credit |
| Original V4 agent messages/schema | Identical before and after this change |
| Owned-local applied agent proposals | 2 of 2 useful after primary source/arithmetic review; one actual call, 564 input tokens |
| First new-document trial | 3 of 3 factual passes; 2 of 3 full presentation/scope passes, unqualified |
| Source-context correction | 1 consumed repair passed; 1 separately frozen new-document case passed |
| Owned runtime cleanup | Both runs independently verified; tracked processes gone and isolated ports closed |

The first trial exposed an excerpt-curation failure: a source's date header was
omitted before inference, leaving historical scope absent from one delivered
answer. That original failure and rubric remain unchanged. The correction keeps
exact source title/date/status preambles with reviewed sections. The repair is
reported as consumed; only the separately frozen document receives fresh-case
credit. Required facts, conditions, source bindings and readable views were
checked in the final two delivered answers.

The two native runs used eleven calls in total: one CLI proposal call plus ten
answer calls for five delivered answers, including the explicit consumed
repair. Each run used a separately frozen package/rubric and bounded watchdog;
there were no automatic generation retries. The first run took 229.43 seconds
and the correction took 124.56 seconds. Only installed owned-local inference was
used. No paid API, cloud CI, release or default promotion occurred.

## Behavior

In the reviewed pilot, opt in to **Review question parts before answering**,
edit one to six parts and explicitly confirm them. Question, part, source,
workspace or model changes invalidate confirmation. Errors retain the edited
parts but require confirmation again. Dispatch is single-flight and does not
silently retry or fall back to the older endpoint.

The new `test-matrix-v5` role proposes hypothetical scalar inputs, a question,
a concrete expected answer and source references. It rejects the observed
instruction-echo shapes. A structurally valid receipt remains `needs_review`:
primary review still checks arithmetic, entailment, conditions and usefulness.
Two useful proposals on one narrow assignment are not general agent reliability
or evidence that tests were executed. No new defects were discovered by that
assignment.

## Limits

All document trials use locally held material from the same company. Document
novelty is checked against the recorded prior corpus, not against model training
or every earlier exposure. Primary wrote and knew the rubrics. New knowledge
approvals are explicitly synthetic AI test fixtures, not owner business-fact
approval. Model source units used the whole-statement fallback. This does not
qualify automatic source planning, unseen companies or large-team operations.

The earlier 69-case qualification remains historical and unchanged. It was not
rerun, rescored or combined with these small trials. Initial checkout/test setup
failures are retained locally: the CLI target now builds its entrypoints before
testing, and Vite permits linked dependency assets without granting access to
the surrounding experiment tree. Private source documents, raw replies,
databases and full evidence archives stay local.
