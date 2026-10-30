## Decision and status

**Rob's decision, October 28, 2026: keep Potluck's core open source while pursuing funding.** Rob requested this detailed free-versus-paid map as the working direction for subsequent builds.

This note translates that decision into a product boundary. It is an internal build policy, not a published price list, signed customer commitment, or claim that every listed capability exists. The core decision is explicit; package names, charging unit, commercial module structure, and pilot gates below are implementation recommendations to validate through use and purchasing evidence.

**Free core:** run useful AI on infrastructure you control, complete a cited investigation, review the outcome, retain reusable knowledge, and evaluate improvements yourself.

**Paid company offering:** deploy, administer, monitor, govern, and improve that system across an organization with less operational work and a defined support relationship.

Company size does not determine whether the core is free. An enterprise may self-host the open core for commercial use. Payment buys additional software or services, not permission to use the existing MIT code at work. Free software does not include free hardware, electricity, third-party APIs, cloud hosting, or other people's computing capacity.

## Commitments to preserve

1. Preserve the existing MIT core and already-open functionality. Do not move existing open features behind a proprietary paywall to create a paid tier.
2. Keep local inference, owned-machine execution, and the complete first Company Brain workflow useful without a subscription or mandatory hosted account for local use.
3. Keep basic authentication, authorization enforcement, source restrictions, deletion, security fixes, provenance, and data export in the core. A free deployment must not be unsafe by design.
4. Do not impose artificial seat, document, token, source, or owned-machine caps on the self-hosted core. Technical capacity and safety limits still apply. Managed services may have transparent resource limits tied to actual operating costs.
5. Keep customer-created knowledge, evaluation datasets, run artifacts, and exportable configurations portable. Customers retain their rights to their data and created artifacts; underlying model and third-party license conditions still apply.
6. No implicit upload of private sources, traces, or training examples. Customer content is not pooled for Potluck training without separate explicit authorization. Access for support is explicit and scoped.
7. Core functionality does not depend on a paid control service remaining available. Permission checks remain enforced during a management outage or subscription lapse; inability to verify authorization fails closed.
8. Describe the product accurately: an open-source core with commercial organizational software/services. Identify any future proprietary component clearly; source-visible proprietary software is not described as open source.

## Exact feature boundary

The table assigns intended packaging when a feature is implemented. It does not make planned features available today.

| Area | Free and open core | Paid company offering | Reason a company might pay |
| --- | --- | --- | --- |
| Models and local clients | Desktop, CLI, local API, MCP, compatible local models, manual model selection and configuration. | Central approved-model catalog, organization rollout controls and fleet configuration. | Keep deployments consistent without manually configuring each machine. |
| Owned compute and mesh | Pair approved machines; execute on selected owned infrastructure; core scheduling, queues and model-splitting implementations as they mature. No commercial-use fee or artificial owned-node cap. | Fleet enrollment campaigns, inventory, health monitoring, capacity reporting, coordinated upgrades and staged rollbacks. | Reduce ongoing administration across machines and sites. |
| Sources and ingestion | Local collections, connector interfaces and connector implementations we release, manual configuration, updates/deletions, source versions, visible errors. | Managed connector configuration, centrally stored secret references, monitored sync schedules, failure alerts and supported connector operation. | Keep sources current without someone tending each deployment. |
| Knowledge lifecycle | Raw evidence, proposed claims, human approval/correction, supersession, ownership metadata and history. | Organization-wide review queues, reviewer assignment, escalation rules and oversight reports. | Make shared maintenance accountable across departments. |
| Investigations and handoffs | Cited briefs, uncertainty, source inspection, saved handoffs, accept/correct/reject, recorded outcomes and reuse. | Administer shared workflow templates and organizational rollout; operational reporting across workspaces. | Standardize a repeated workflow and see where it succeeds or needs attention. |
| Workspace collaboration | Basic shared-workspace operation when implemented, manual member/role management, server-side scope enforcement and permission-preserving derived artifacts. | Directory-driven provisioning, group synchronization, delegated administration and centrally managed policy inheritance. | Avoid manual access administration as teams change. |
| Identity and security | Local/device authentication, sessions and revocation, basic roles, supported transport protection, credential handling and security fixes. A basic standards-based login adapter belongs here when implemented. | SCIM/directory lifecycle automation, multi-directory configuration, organization-wide access review and policy administration. | Coordinate joiners, role changes, departures and oversight. SSO alone is not the business model. |
| Access and action policies | Enforcement engine; source permissions; explicit routing scope; action allowlists; local human confirmation and denial of unauthorized work. | Central policy distribution, managed exceptions, multi-stage approval configuration and delegated policy ownership. | Apply and review consistent rules across deployments. Enforcement itself stays free. |
| Audit and provenance | Local actor/action/source/model records, review history, security-relevant events and ordinary export. | Cross-deployment audit search, scheduled evidence reports, managed retention policies and external audit-log integrations. | Answer organization-wide oversight questions without assembling records by hand. |
| Evaluation | Create/export cases, freeze holdouts, run local comparisons, inspect failures and record quality/cost/latency. | Scheduled fleet/workflow evaluations, regression alerts, approval-gated promotion and centralized comparisons. | Catch regressions routinely and coordinate releases. A user may automate the open CLI independently. |
| Model improvement | Open evaluation formats and local improvement tools we build; manual candidate testing and loading, including adaptation only where justified. | Managed improvement jobs, versioned organizational approvals, monitored deployment and rollback. | Operate a repeatable improvement process rather than maintain scripts. No quality improvement is guaranteed. |
| Backup, retention and portability | Export/import, documented backup/restore, local retention and deletion controls, customer-accessible data formats. | Managed backup scheduling, restore checks, retention administration and supported recovery procedures. | Reduce operational work and establish tested recovery practices. |
| Hosting and deployment | Self-managed installation on supported customer hardware or infrastructure. | Supported private deployment and, later, optional managed infrastructure. Commercial management can run in the customer's environment. | Purchase deployment and operating convenience without requiring private data to move to Potluck's cloud. |
| Support | Documentation, public issue reporting, community help and public security fixes. No response-time promise. | A defined supported configuration, onboarding scope, support hours and response commitments we can meet. | Have an accountable vendor for an operational dependency. |
| External capacity | Open routing/client components retain their license; using one's own machines stays free. | Third-party capacity, hosted services or public-pool consumption may have separately disclosed charges under a future approved offer. | Buy actual resources. Tokens, rewards and public-pool pricing are separate unresolved work. |

The boundary is additional administration and service responsibility. Shared knowledge, basic collaboration, model splitting, citations, human review, and useful local evaluations are not automatically paid just because a company uses them.

## Packages and charging approach

Rob subsequently approved using these package names in the website preview, with **Core $0 and company plans by discussion**. Company and Enterprise remain marked planned/in development. Rob subsequently approved publication; website commit `20434d3` is live, including [pricing](https://trypotluck.ai/pricing). No numeric paid prices or checkout are authorized by this note.

| Package | Offer | Packaging direction |
| --- | --- | --- |
| **Potluck Core** | Complete self-managed foundation and useful workflows, including the free column above. | $0 software license fee for the core. Individual and commercial organizational use permitted under its license. Customer supplies resources and operates it. |
| **Potluck Company** | Commercial management software plus a bounded supported private deployment: source/job health, approved model/configuration rollout, organizational oversight and support as those capabilities are delivered. | Recurring subscription. Initial pricing hypothesis: base fee per managed deployment/organization with included managed capacity and explicit extra service/resource costs. Do not meter locally generated tokens or tax every reader of company knowledge. Price and unit must be tested against value and support cost. |
| **Enterprise deployment options** | Directory automation, multi-site administration, offline management, advanced audit integrations and broader support requirements. | Expansion of the same company product, with a scoped commercial agreement. Build only when a credible deployment requires it. No automatic 24/7 SLA, compliance certification or custom infrastructure promise. |

Managed capacity is an accounting unit for the paid service we operate or support; it does not impose limits on MIT core installations. Commercial software can be licensed for customer-hosted deployment. Selling managed cloud hosting is optional, not a requirement of the ownership thesis.

Self-hosting teams can build substitutes from the open interfaces. That is an accepted tradeoff. The product must save enough recurring engineering and operational work that paying is preferable to maintaining the substitute.

## First offer to validate

**A bounded, supported private engineering-investigation pilot.** Buyer hypothesis: an engineering/platform leader responsible for an expensive recurring investigation and the system that supports it. A department inside a large enterprise is a valid starting customer.

- One organization, one agreed workflow and supported deployment configuration, one approved model configuration, and one useful source integration or import path.
- Deliver the cited brief → reviewer correction → recorded outcome → reusable knowledge/evaluation loop from the open core.
- Add the minimum repeatable company operations: source/job health view, freshness/failure alerts, controlled configuration rollout and a defined support route. Implement these before promising them as available.
- Include a documented install/upgrade/restore process and a short operating report. Authentication, workspace authorization, source lifecycle and export are prerequisites, including in the free core.
- Agree baseline, success measures, scope, term and fee before a paid pilot begins. A paid pilot fee is a validation offer, not evidence of willingness to pay until a buyer accepts it.

The economic hypothesis is fewer hours gathering context and keeping the system working, with reviewable evidence and customer-controlled execution. The pilot must test both workflow usefulness and the reason to pay for company operations. Initial setup services can be scoped separately, but the business should converge on repeatable software and recurring operations rather than unlimited custom implementation.

Measure time to an accepted investigation brief, correctness and citation support, reviewer corrections, repeat use, source freshness, setup/maintenance effort, and founder support hours. Compare with the team's existing process. A successful demo or a GitHub star does not establish conversion or retention.

## How this changes the build plan

| Stage | Work and packaging | Gate |
| --- | --- | --- |
| Now: complete the first batch | All five deliverables in [[Potluck First Company Brain Build Batch]] are free core: source collections, reviewed knowledge/supersession, cited evidence lookup, investigation/handoff, review/evaluation. | Complete workflow and independent quality/scope checks. No billing or entitlement service dependency. |
| Next: safe private pilot foundation | Authentication/authorization, source lifecycle, export/restore, reproducible deployment and one source integration belong in core where they provide baseline functionality. | Safe, repeatable operation; no customer multi-user rollout before authorization is enforced. |
| First commercial increment | Company operations for that same supported configuration: monitored sources/jobs, centrally managed model/configuration changes and defined support. Keep additional admin workflows separate from core enforcement. | Independent repeat users and an accepted paid scope; demonstrate value and record support cost. |
| Expansion | Directory lifecycle, policy administration, larger fleets, scheduled evaluation promotion and advanced audit integrations. | Buyer-backed operational need and evidence that the deployment can be supported sustainably. |
| Later specialization | Managed adaptation and more ambitious model improvement, public-pool capacity or broader deployment options. | Measured task improvement or capacity need, viable cost, appropriate data rights and separate operational readiness. |

Current recorded implementation: source collections, reviewed knowledge/supersession and cited lookup are locally verified through `6d404dc`; the investigation/handoff and review/evaluation deliverables remain in progress. See [[Potluck Company Brain Build Progress]] for the current source of truth. Commercial management, enterprise readiness and paid adoption are not established by this policy.
