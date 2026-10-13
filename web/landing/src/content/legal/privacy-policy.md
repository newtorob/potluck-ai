# Privacy Policy

**Last updated: September 16, 2026**

Coshippr Inc., doing business as Potluck ("Potluck," "we," "us") builds local-first AI software. This policy explains what we collect, why, and what we do not. We wrote it to be read, not to hide behind.

## The short version

Local inference runs on your computer. Your chat history, saved memories, and tasks are stored locally. If you choose a feature that sends content to another machine, that machine receives the content needed to perform the work. Our hosted services also handle account information and network coordination metadata. The route you choose matters; the details are below.

## 1. What this covers

This policy covers the Potluck desktop application, the website at trypotluck.ai, and the account, membership, billing, and network coordination services we run. It does not cover third parties we link to or integrate with, who have their own policies (see Section 6).

Potluck is early software under active development. Practices may change as the product develops; we will update this policy and the "last updated" date when they do.

## 2. What we collect

**Account information.** When you create an account we collect your email address (used for passwordless magic-link sign-in) and, if you set one, a display name. We store a hashed session token so you stay signed in.

**Membership and billing.** If you become a member or pay dues, our payments processor (Stripe) handles your card details. We do not see or store full card numbers. We keep a record of your membership status, plan, and billing events (for example, a successful payment or a cancellation).

**Network coordination.** When you connect machines, the coordinator handles enrollment and connection metadata, such as machine identifiers, public keys, network endpoints, and advertised capabilities. This is distinct from the content carried inside an encrypted WireGuard connection.

**Technical and security data.** Our servers log basic request metadata (such as IP address and timestamps) to operate the service, prevent abuse, and rate-limit sign-in requests. If you opt into error reporting, our error-monitoring tool may capture technical diagnostics; we mask email addresses in those logs.

**Community.** If you use our community forum, that account and your posts are handled there (see Section 6).

**Website.** The website uses privacy-respecting, cookieless analytics that do not track you across sites or build an advertising profile. Download links record the selected platform and a timestamp to count downloads. Account sign-in begins in the desktop app.

## 3. What we do NOT collect

For local inference, Potluck's hosted services do not receive your prompt or generated answer. Your chat history, memories, and task notes are stored on your machine. Downloading a model does not upload your local files to us.

These statements describe local use. They are not a claim that content can never leave your machine: remote inference and optional peer memory sharing send content to the destination you choose.

## 4. The mesh: what leaves your machine

The current desktop release supports local inference and inference across your own paired machines. Trusted-circle inference and the open contributor pool remain in development. See the [roadmap](https://trypotluck.ai/roadmap) for availability.

- **Local inference.** The prompt is processed on this computer. Other app activities, such as downloading models, checking for updates, signing in, or joining the mesh, may still make network requests.
- **Your machines.** Your request can be processed on a paired machine over an encrypted WireGuard connection. A relay carrying this connection sees encrypted traffic; the machine generating the answer processes the prompt in plaintext. Only pair machines you control and trust.
- **Optional peer memory sharing.** When enabled on a machine, paired peers can search that machine's saved memories through read-only endpoints. Sharing is currently per machine, not per project. This does not automatically synchronize every machine's memory store.
- **Contributor-network routing under development.** The current coordinator-based design sends the prompt through the coordinator to a serving contributor. The coordinator therefore processes prompt content. The job sent to the contributor omits account identity, but the prompt itself could identify you. This is not end-to-end encrypted inference or a guarantee of anonymity. Rules against retaining requests cannot prove that a modified contributor does not record them.

## 5. How we use information

We use the information above to:

- Provide and maintain the service, including signing you in and running the network.
- Process membership and payments.
- Keep the service secure, prevent abuse, and debug problems.
- Communicate with you about your account, service changes, and support requests.

We do not sell your personal information. We do not use your content to train models without your explicit opt-in.

## 6. Third parties we rely on

We use a small number of service providers to run Potluck. Each processes only what it needs and under its own terms:

- **Stripe** — payments and billing.
- **Resend** — sending transactional email (such as your sign-in link).
- **Sentry** — error monitoring (email addresses masked), if enabled.
- **Discourse** — the community forum, if you use it.
- **Vercel** — hosting the trypotluck.ai website and providing cookieless website analytics.
- **Supabase** — storing anonymous download counts (platform and timestamp).
- **Fly.io / DigitalOcean** — hosting our account and coordination services.
- **Cloudflare** — DNS, content delivery, and release-file hosting.

This list may change as the service grows; we will keep it current.

## 7. Cookies and tracking

We use a strictly necessary session cookie (or an equivalent token) to keep you signed in to your account. The website uses cookieless analytics and does not use advertising or cross-site tracking cookies.

## 8. Data retention

We keep account and billing records for as long as you have an account and as long as we are required to for legal, tax, and accounting purposes. Security logs are kept for a limited period and then discarded. Memory and content stored on your device are retained until you delete them, which you can do directly in the app.

## 9. Your rights and choices

You can:

- **Access or correct** your account information by contacting us.
- **Delete** your account by contacting us; deleting the app and its local data removes the on-device parts yourself.
- **Manage billing** through the Stripe customer portal linked in the app.

Depending on where you live, you may have additional rights under laws such as the GDPR (EU/UK) or the CCPA/CPRA (California), including rights to access, delete, correct, or port your personal information, and to object to certain processing. We honor these requests and do not discriminate against you for exercising them. To make a request, contact us at the address below.

## 10. Security

We use encryption in transit, hashed session tokens, and access controls, and we designed the product to minimize what we hold in the first place. No system is perfectly secure, but keeping the sensitive data on your device is our main protection.

## 11. Children

Potluck is not directed to children under 13 (or the minimum age in your country), and we do not knowingly collect their personal information.

## 12. International users

We operate from the United States. If you use Potluck from outside the US, you understand that the limited account and billing data we hold is processed in the US and other countries where our providers operate.

## 13. Changes to this policy

We will update this policy as the product and our practices change, and we will revise the "last updated" date. For material changes we will make a reasonable effort to notify members.

## 14. Contact

Questions or requests: **hello@trypotluck.ai**
Coshippr Inc., doing business as Potluck, a Delaware corporation, c/o Legalinc Corporate Services Inc., 131 Continental Drive, Suite 305, Newark, DE 19713.
