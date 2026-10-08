import { test, expect, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { mockBackends, ADMIN } from './mock';

// Trusted circles from both sides, in the real app in a browser: the host
// choosing who can use the machine, the requester seeing what the circle can
// run, and the Circles page where people get in by invite. Every backend
// request is intercepted; nothing reaches a real engine or coordinator. The
// coordinator is an in-memory fake of the circle and invite routes.
// Screenshots land in OUTPUT for eyeballing.
const OUTPUT = process.env.POTLUCK_UI_REVIEW_DIR ?? '/tmp/potluck-ui-review-circles';
const COORDINATOR = 'https://coordinator.example.test';
const CIRCLE_ID = '11111111-1111-4111-8111-111111111111';
const LEGACY_KEY = 'ExampleDevicePublicKeyAAAAAAAAAAAAAAAAAAAAA';
const INVALID_INVITE = 'That invite is not valid. It may have expired or already been used. Ask for a new one.';
const CONSENT =
  'Joining lets the people in this circle send requests to your machines that share with trusted circles, and lets you send requests to theirs. The machine that answers sees the conversation. You can leave at any time.';

interface FakePerson {
  id: string;
  name: string;
  role: 'owner' | 'member';
  is_you: boolean;
  joined_at: string;
}

interface FakeCircle {
  id: string;
  name: string;
  created_at: string;
  // Left out to play an older coordinator, which sends neither.
  role?: 'owner' | 'member';
  people?: FakePerson[];
  members: { member_pubkey: string; added_at: string }[];
}

interface FakeInvite {
  id: string;
  circle_id: string;
  code: string;
  created_at: string;
  expires_at: string;
  max_uses: number;
  uses: number;
}

// An invite that exists for a circle the signed-in member is not in yet.
interface Joinable {
  invited_by: string;
  // The circle as the member will see it once they have joined, without their own row.
  circle: FakeCircle;
  full?: boolean;
}

const DAY = 24 * 60 * 60 * 1000;

function person(id: string, name: string, role: FakePerson['role'] = 'member', isYou = false): FakePerson {
  return { id, name, role, is_you: isYou, joined_at: '2026-10-01T12:00:00Z' };
}

/** The one owned circle with a pasted machine key that the older tests rely on, in the older coordinator shape. */
function legacyCircle(): FakeCircle {
  return {
    id: CIRCLE_ID, name: 'Friends', created_at: '2026-10-07T12:00:00Z',
    members: [{ member_pubkey: LEGACY_KEY, added_at: '2026-10-07T12:01:00Z' }],
  };
}

function ownedCircle(): FakeCircle {
  return {
    id: '22222222-2222-4222-8222-222222222222', name: 'Home lab', created_at: '2026-10-01T12:00:00Z', role: 'owner',
    people: [person('p-rob', 'Rob', 'owner', true), person('p-ada', 'Ada'), person('p-grace', 'Grace')],
    members: [],
  };
}

function memberCircle(): FakeCircle {
  return {
    id: '33333333-3333-4333-8333-333333333333', name: 'Potluck Lab', created_at: '2026-09-20T12:00:00Z', role: 'member',
    people: [person('p-ada', 'Ada', 'owner'), person('p-rob', 'Rob', 'member', true), person('p-lin', 'Lin')],
    members: [],
  };
}

function normalizeCode(raw: unknown): string | null {
  const match = String(raw ?? '').match(/([A-Za-z0-9]{4})[-\s]?([A-Za-z0-9]{4})[-\s]?([A-Za-z0-9]{4})\s*$/);
  return match ? `${match[1]}-${match[2]}-${match[3]}`.toUpperCase() : null;
}

/**
 * In-memory stand-in for the coordinator's circle routes. `circles` is what
 * GET /me/circles returns for the signed-in member (named Rob); `calls` logs
 * every request so a test can say what was and was not sent.
 */
function fakeCoordinator(init: { circles?: FakeCircle[]; joinable?: Record<string, Joinable> } = {}) {
  const codes = ['K7QM-4TXP-9RWD', 'H2VN-8BCF-3ZLE', 'P5GT-6YJA-2NXD'];
  let sequence = 100;
  const nextId = () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}`;
  const state = {
    circles: init.circles ?? [legacyCircle()],
    invites: [] as FakeInvite[],
    joinable: init.joinable ?? {},
    calls: [] as string[],
    // The next request that matches fails once with this status and detail.
    failNext: null as { method: string; path: RegExp; status: number; detail: string } | null,
  };

  const find = (id: string | undefined) => state.circles.find((c) => c.id === id);
  const view = (c: FakeCircle) => ({ ...c, members: c.role === 'member' ? [] : c.members });
  const isOwner = (c: FakeCircle) => (c.role ?? 'owner') === 'owner';

  function handle(method: string, path: string, body: Record<string, unknown>): { status: number; json?: unknown } | null {
    if (state.failNext && state.failNext.method === method && state.failNext.path.test(path)) {
      const { status, detail } = state.failNext;
      state.failNext = null;
      return { status, json: { detail } };
    }
    if (path === '/me/circles' && method === 'GET') return { status: 200, json: { circles: state.circles.map(view) } };
    if (path === '/me/circles' && method === 'POST') {
      const created: FakeCircle = {
        id: nextId(), name: String(body['name']), created_at: new Date().toISOString(), role: 'owner',
        people: [person(nextId(), 'Rob', 'owner', true)], members: [],
      };
      state.circles.push(created);
      return { status: 201, json: created };
    }
    if (path === '/circles/invites/preview' || path === '/circles/invites/accept') {
      const code = normalizeCode(body['code']);
      const entry = code === null ? undefined : state.joinable[code];
      if (!entry) return { status: 404, json: { detail: INVALID_INVITE } };
      const already = state.circles.some((c) => c.id === entry.circle.id);
      if (path.endsWith('/preview')) {
        return { status: 200, json: {
          circle_name: entry.circle.name, invited_by: entry.invited_by,
          people: (entry.circle.people ?? []).length, expires_at: new Date(Date.now() + 3 * DAY).toISOString(), already_in: already,
        } };
      }
      if (entry.full) return { status: 409, json: { detail: 'This circle is full.' } };
      if (!already) {
        state.circles.push({ ...entry.circle, role: 'member', people: [...(entry.circle.people ?? []), person('p-rob', 'Rob', 'member', true)] });
      }
      return { status: 200, json: view(find(entry.circle.id)!) };
    }

    const match = path.match(/^\/me\/circles\/([^/]+)(?:\/(invites|membership|people|members)(?:\/([^/]+))?)?$/);
    if (!match) return null;
    const [, circleId, part, tail] = match;
    const circle = find(circleId);
    if (!circle) return { status: 404, json: { detail: 'Circle not found.' } };

    if (part === undefined && method === 'DELETE') {
      state.circles = state.circles.filter((c) => c.id !== circle.id);
      return { status: 204 };
    }
    if (part === 'membership' && method === 'DELETE') {
      if (isOwner(circle)) return { status: 409, json: { detail: 'You own this circle. Delete it to end it for everyone.' } };
      state.circles = state.circles.filter((c) => c.id !== circle.id);
      return { status: 204 };
    }
    if (!isOwner(circle)) return { status: 403, json: { detail: 'Only the owner can do that.' } };
    if (part === 'people' && method === 'DELETE') {
      circle.people = (circle.people ?? []).filter((p) => p.id !== tail);
      return { status: 204 };
    }
    if (part === 'members' && method === 'DELETE') {
      circle.members = circle.members.filter((m) => m.member_pubkey !== tail);
      return { status: 204 };
    }
    if (part === 'invites' && method === 'POST') {
      const code = codes[state.invites.length % codes.length]!;
      const invite: FakeInvite = {
        id: nextId(), circle_id: circle.id, code, created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 7 * DAY).toISOString(), max_uses: Number(body['max_uses'] ?? 1), uses: 0,
      };
      state.invites.push(invite);
      return { status: 201, json: { id: invite.id, code, url: `https://trypotluck.ai/circles/join#code=${code}`, expires_at: invite.expires_at, max_uses: invite.max_uses } };
    }
    if (part === 'invites' && method === 'GET') {
      // Only invites that can still be accepted, and never their codes.
      return { status: 200, json: state.invites.filter((i) => i.circle_id === circle.id && i.uses < i.max_uses)
        .map(({ id, created_at, expires_at, max_uses, uses }) => ({ id, created_at, expires_at, max_uses, uses })) };
    }
    if (part === 'invites' && method === 'DELETE') {
      state.invites = state.invites.filter((i) => i.id !== tail);
      return { status: 204 };
    }
    return null;
  }

  return { state, handle };
}

type FakeCoordinator = ReturnType<typeof fakeCoordinator>;

interface Scenario {
  audience: 'mine' | 'circles' | null;
  contributor: Record<string, unknown>;
  circleModels: { slug: string; machines: number }[];
  coordinator?: FakeCoordinator;
}

async function desktop(page: Page, scenario: Scenario) {
  const coordinator = scenario.coordinator ?? fakeCoordinator();
  const appOrigin = new URL(test.info().project.use.baseURL as string).origin;
  const patches: unknown[] = [];
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin === appOrigin) return route.continue();
    return route.fulfill({ status: 503, json: { detail: 'Unavailable in UI test' } });
  });
  await mockBackends(page, { me: ADMIN });
  await page.addInitScript(() => {
    localStorage.setItem('potluck.autoload_on_startup', 'false');
    localStorage.setItem('potluck.contribution_receipts_enabled', 'false');
    localStorage.setItem('potluck.routing_scope', 'circle');
    (window as unknown as { potluck: unknown }).potluck = {
      platform: 'darwin', arch: 'arm64', getAppVersion: async () => '0.1.9',
      mesh: { request: async (_method: string, path: string) => ({ status: 200, body: path === '/status' ? {
        mesh_status: 'running', node_id: 'self', household_id: 'home', mesh_ip: '100.64.0.1',
        coordinator_connection: 'connected', coordinator_url: 'https://coordinator.example.test',
        peers_online_count: 0, last_error: null, vpn_conflict: false, vpn_detail: '', wg_port_listening: true,
      } : { peers: [] } }) },
    };
  });
  const settings = {
    runtime: { temperature: 0.7, max_tokens: 2048, context_size: 8192, gpu_layers: -1, threads: 8 },
    privacy: { chat_history_enabled: false, telemetry_enabled: false, crash_reports_enabled: false, memory_enabled: true },
    network: { contributor_mode: true, contributor_audience: scenario.audience, max_concurrent_jobs: 1, coordinator_url: COORDINATOR },
    app: { theme: 'dark', font_size: 15 }, memory: { enabled: true, auto_extract: false, auto_retrieve: true, extraction_frequency: 5, max_memories: 1000, max_retrieval: 5, min_confidence_for_retrieval: 0.5, retention_days: 0, categories_enabled: [] },
    gateway: { enabled: false, api_key: '', pool_routing: false }, vault: { folder: null, last_scan_at: null, file_count: 0, chunk_count: 0 }, ui: {}, active_project: null, models_path: '', active_model_id: null,
  };
  await page.route(/:8321\//, (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/settings' && request.method() === 'PATCH') {
      const patch = request.postDataJSON() as { network?: Record<string, unknown> };
      patches.push(patch);
      Object.assign(settings.network, patch.network ?? {});
    }
    const data: Record<string, unknown> = {
      '/models': [], '/health': { status: 'ok', model_loaded: true, active_model_id: 'qwen3-4b-instruct-2507-q4', n_ctx_loaded: 8192 },
      '/hardware': { cpu_name: 'Apple M2', ram_total_gb: 16, ram_available_gb: 9, gpu_name: 'Apple M2', gpu_vram_gb: 16, recommended_tier: 'medium' },
      '/settings': settings, '/memory/projects': [], '/conversations': [],
      '/contributor/status': scenario.contributor,
      '/contributor/device-key': { public_key: 'ExampleDevicePublicKeyAAAAAAAAAAAAAAAAAAAAA' },
    };
    return route.fulfill({ json: data[path] ?? [], headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' } });
  });
  await page.route(`${COORDINATOR}/**`, (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const cors = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    coordinator.state.calls.push(`${request.method()} ${path}`);
    if (path === `/me/circles/${CIRCLE_ID}/availability`) {
      return route.fulfill({ headers: cors, json: { circle_id: CIRCLE_ID, models: scenario.circleModels } });
    }
    if (path === '/me/device-keys') return route.fulfill({ status: 201, headers: cors, json: {} });
    const body = request.method() === 'POST' ? (request.postDataJSON() as Record<string, unknown> | null) ?? {} : {};
    const reply = coordinator.handle(request.method(), path, body);
    if (reply === null) return route.fulfill({ status: 503, headers: cors, json: { detail: 'Unavailable in UI test' } });
    if (reply.status === 204) return route.fulfill({ status: 204, headers: cors });
    return route.fulfill({ status: reply.status, headers: cors, json: reply.json });
  });
  await page.setViewportSize({ width: 1152, height: 768 });
  await mkdir(OUTPUT, { recursive: true });
  return patches;
}

async function capture(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${OUTPUT}/${name}.png`, animations: 'disabled' });
}

const connected = {
  enabled: true, running: true, state: 'connected', peer_id: 'p', connected_since: null, last_heartbeat_at: null,
  heartbeats_sent: 4, reconnects: 0, jobs_served: 3, jobs_in_flight: 0, last_error: null, last_error_detail: null,
  coordinator_host: 'coordinator.example.test', binary_hash_source: 'file', max_concurrent_jobs: 1,
  has_member_session: true, audiences: ['circle'],
};

test('a host chooses who can use the machine and sees who it is shared with', async ({ page }) => {
  const patches = await desktop(page, { audience: null, contributor: { ...connected, state: 'disabled', audiences: null }, circleModels: [] });
  await page.goto('/#/network');
  const group = page.getByRole('group', { name: 'Who can use this machine' });
  await expect(group).toBeVisible();
  // Nothing chosen in a release build: only the member's own machines, and no
  // connection line because the engine has not joined the coordinator.
  await expect(group.getByRole('radio', { name: /Only my own machines/ })).toBeChecked();
  await expect(group.getByRole('radio', { name: /Everyone on the network/ })).toHaveCount(0);
  await expect(page.getByText(/Sharing with your trusted circles/)).toHaveCount(0);
  await capture(page, 'network-sharing-mine');

  await group.getByRole('radio', { name: /My trusted circles too/ }).check();
  await expect(group.getByRole('radio', { name: /My trusted circles too/ })).toBeChecked();
  expect(patches).toContainEqual({ network: { contributor_audience: 'circles' } });
});

test('a machine shared with circles says so under the toggle', async ({ page }) => {
  await desktop(page, { audience: 'circles', contributor: connected, circleModels: [] });
  await page.goto('/#/network');
  const group = page.getByRole('group', { name: 'Who can use this machine' });
  await expect(group.getByRole('radio', { name: /My trusted circles too/ })).toBeChecked();
  await expect(group).toContainText('It is never offered open pool work.');
  await expect(page.getByRole('status').filter({ hasText: 'Sharing with your trusted circles' })).toContainText('3 jobs served');
  await capture(page, 'network-sharing-circles');
});

test('a host is told when the network cannot yet limit sharing to circles', async ({ page }) => {
  await desktop(page, { audience: 'circles', circleModels: [],
    contributor: { ...connected, state: 'rejected', last_error: 'coordinator_outdated', audiences: ['circle'] } });
  await page.goto('/#/network');
  await expect(page.getByRole('status').filter({ hasText: 'nothing is being shared' })).toBeVisible();
  await capture(page, 'network-sharing-outdated');
});

test('a requester sees the models the circle can run and asks for one of them', async ({ page }) => {
  await desktop(page, { audience: 'mine', contributor: connected,
    circleModels: [{ slug: 'qwen3-4b-instruct-2507-q4', machines: 2 }, { slug: 'gemma-3-12b-it-q4', machines: 1 }] });
  await page.goto('/#/chat');
  const picker = page.getByRole('combobox', { name: 'Model this circle will answer with' });
  await expect(picker).toBeVisible();
  await expect(picker).toHaveValue('qwen3-4b-instruct-2507-q4');
  await expect(picker.getByRole('option')).toHaveText(['qwen3-4b-instruct-2507-q4 (2 machines)', 'gemma-3-12b-it-q4']);
  await picker.selectOption('gemma-3-12b-it-q4');
  await expect(picker).toHaveValue('gemma-3-12b-it-q4');
  await capture(page, 'chat-circle-models');
});

test('a requester is told when no circle machine is sharing', async ({ page }) => {
  await desktop(page, { audience: 'mine', contributor: connected, circleModels: [] });
  await page.goto('/#/chat');
  await expect(page.getByText('No machine in this circle is sharing a model right now.')).toBeVisible();
  await capture(page, 'chat-circle-empty');
});

// ---- The Circles page: invites, joining, leaving, removing ----------------

/** Open /#/circles against a fake coordinator. Taller than the other pages so a whole open card fits in a screenshot. */
async function openCircles(page: Page, init: Parameters<typeof fakeCoordinator>[0] = {}) {
  const coordinator = fakeCoordinator(init);
  await desktop(page, { audience: 'circles', contributor: connected, circleModels: [], coordinator });
  await page.setViewportSize({ width: 1152, height: 1100 });
  await page.goto('/#/circles');
  await expect(page.getByRole('heading', { name: 'Trusted Circles' })).toBeVisible();
  return coordinator.state;
}

/** The list item for one circle, found by the button that opens it. */
function cardOf(page: Page, name: string) {
  return page.getByRole('listitem').filter({ has: page.getByRole('button', { name: new RegExp(`^${name}`) }) });
}

async function openCard(page: Page, name: string) {
  const card = cardOf(page, name);
  await card.getByRole('button', { name: new RegExp(`^${name}`), expanded: false }).click();
  return card;
}

test('the Circles page points to Network for turning sharing on', async ({ page }) => {
  await openCircles(page, { circles: [ownedCircle()] });
  const line = page.locator('p', { hasText: 'To let a circle use this machine' });
  await expect(line).toHaveText('To let a circle use this machine, turn on Share spare compute under Network and choose My trusted circles too.');
  await expect(line.getByRole('link', { name: 'Network' })).toHaveAttribute('href', '#/network');
  await expect(page.getByText('Your key')).toHaveCount(0);
  await expect(page.getByPlaceholder(/public key/i)).toHaveCount(0);
});

test('an owner makes an invite, sees the code once, and can withdraw it', async ({ page }) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  const coordinator = await openCircles(page, { circles: [ownedCircle()] });
  await expect(page.getByText('You own this circle')).toBeVisible();
  const card = await openCard(page, 'Home lab');

  // Everyone in the circle is listed. The owner can remove anyone but themselves.
  const people = card.getByRole('list', { name: 'People in this circle' });
  await expect(people.getByRole('listitem')).toHaveCount(3);
  await expect(people.getByRole('listitem').nth(0)).toContainText('Rob');
  await expect(people.getByRole('listitem').nth(0)).toContainText('(you)');
  await expect(people.getByRole('listitem').nth(0)).toContainText('Owner');
  await expect(card.getByRole('button', { name: 'Remove Rob' })).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Remove Ada' })).toBeVisible();
  await expect(card.getByText('No open invites.')).toBeVisible();

  await card.getByRole('button', { name: 'Invite someone' }).click();
  const code = card.getByRole('textbox', { name: 'Invite code' });
  await expect(code).toHaveValue('K7QM-4TXP-9RWD');
  await expect(card.getByRole('textbox', { name: 'Invite link' })).toHaveValue('https://trypotluck.ai/circles/join#code=K7QM-4TXP-9RWD');
  await expect(card.getByText('This code works once and is shown only now. Send it to one person you trust.')).toBeVisible();
  await expect(card.getByText(/^Expires [A-Z][a-z]{2} \d{1,2}, \d{4}$/)).toBeVisible();

  await card.getByRole('button', { name: 'Copy code' }).click();
  await expect(card.getByRole('button', { name: 'Copied' })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('K7QM-4TXP-9RWD');
  await expect(card.getByRole('button', { name: 'Copy link' })).toBeVisible();

  // The open list shows the invite without its code.
  const open = card.getByRole('list', { name: 'Open invites' });
  await expect(open.getByRole('listitem')).toHaveCount(1);
  await expect(open).toContainText('0 of 1 used');
  await expect(open).not.toContainText('K7QM');
  await capture(page, 'circles-owner-invite');

  // Closing the card forgets the code for good. The invite stays open.
  await card.getByRole('button', { name: /^Home lab/ }).click();
  await card.getByRole('button', { name: /^Home lab/ }).click();
  await expect(card.getByRole('textbox', { name: 'Invite code' })).toHaveCount(0);
  await expect(card.getByRole('list', { name: 'Open invites' }).getByRole('listitem')).toHaveCount(1);

  await card.getByRole('button', { name: 'Withdraw' }).click();
  await expect(card.getByText('No open invites.')).toBeVisible();
  expect(coordinator.invites).toHaveLength(0);
  expect(coordinator.calls.filter((c) => c.startsWith('DELETE') && c.includes('/invites/'))).toHaveLength(1);
});

test('a new circle opens ready for an invite and shows only its owner', async ({ page }) => {
  const coordinator = await openCircles(page, { circles: [] });
  await expect(page.getByText('No circles yet. Create one and invite someone, or join one with an invite code.')).toBeVisible();

  await page.getByRole('textbox', { name: 'New circle name' }).fill('Studio');
  await page.getByRole('button', { name: 'Create' }).click();

  const card = cardOf(page, 'Studio');
  await expect(card).toContainText('You own this circle');
  await expect(card.getByRole('button', { name: 'Invite someone' })).toBeVisible();
  const people = card.getByRole('list', { name: 'People in this circle' });
  await expect(people.getByRole('listitem')).toHaveCount(1);
  await expect(people).toContainText('(you)');
  expect(coordinator.circles).toHaveLength(1);
});

test('a member pastes an invite link, reads what joining means, and joins', async ({ page }) => {
  const circle = memberCircle();
  const coordinator = await openCircles(page, {
    circles: [],
    joinable: { 'HK3F-7MQP-2XVD': { invited_by: 'Ada', circle: { ...circle, people: [person('p-ada', 'Ada', 'owner'), person('p-lin', 'Lin')] } } },
  });
  const input = page.getByRole('textbox', { name: 'Invite code or link' });
  await expect(input).toHaveAttribute('placeholder', 'Paste an invite code or link');
  await expect(page.getByRole('button', { name: 'Check invite' })).toBeDisabled();

  // Pasting alone joins nothing. Cancel leaves everything as it was.
  await input.fill('  https://trypotluck.ai/circles/join#code=hk3f-7mqp-2xvd  ');
  await page.getByRole('button', { name: 'Check invite' }).click();
  const preview = page.getByRole('region', { name: 'Invite preview' });
  await expect(preview).toContainText('Potluck Lab');
  await expect(preview).toContainText('Invited by Ada');
  await expect(preview).toContainText('2 people in this circle');
  await expect(preview).toContainText(CONSENT);
  await expect(preview).toBeFocused();
  await capture(page, 'circles-join-preview');
  await preview.getByRole('button', { name: 'Cancel' }).click();
  await expect(preview).toHaveCount(0);
  await expect(input).toHaveValue('');
  expect(coordinator.calls).not.toContain('POST /circles/invites/accept');

  await input.fill('hk3f-7mqp-2xvd');
  await page.getByRole('button', { name: 'Check invite' }).click();
  await preview.getByRole('button', { name: 'Join circle' }).click();

  await expect(preview).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Join a circle' }).getByRole('status')).toHaveText('You joined Potluck Lab.');
  await expect(input).toHaveValue('');
  expect(coordinator.calls.filter((c) => c === 'POST /circles/invites/accept')).toHaveLength(1);

  const card = cardOf(page, 'Potluck Lab');
  await expect(card).toContainText('You are a member');
  await expect(card.getByRole('button', { name: 'Leave circle Potluck Lab' })).toBeVisible();
  await expect(card.getByRole('button', { name: /^Delete circle/ })).toHaveCount(0);

  // A member sees the people, and can change none of them or make invites.
  await card.getByRole('button', { name: /^Potluck Lab/ }).click();
  const people = card.getByRole('list', { name: 'People in this circle' });
  await expect(people.getByRole('listitem')).toHaveCount(3);
  await expect(people.getByRole('listitem').nth(0)).toContainText('Owner');
  await expect(people).toContainText('Rob');
  await expect(card.getByRole('button', { name: /^Remove/ })).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Invite someone' })).toHaveCount(0);
  await capture(page, 'circles-member-card');
});

test('a member who leaves is told what changes and the circle goes away', async ({ page }) => {
  const circle = memberCircle();
  const coordinator = await openCircles(page, { circles: [circle, ownedCircle()] });
  const card = cardOf(page, 'Potluck Lab');

  await card.getByRole('button', { name: 'Leave circle Potluck Lab' }).click();
  await expect(card).toContainText('People in this circle will no longer be able to use your machines, and you will no longer be able to use theirs.');
  // Cancel keeps the circle and puts focus back on the control that was used.
  await card.getByRole('button', { name: 'Cancel leave' }).click();
  await expect(card.getByRole('button', { name: 'Leave circle Potluck Lab' })).toBeFocused();
  expect(coordinator.calls).not.toContain(`DELETE /me/circles/${circle.id}/membership`);

  await card.getByRole('button', { name: 'Leave circle Potluck Lab' }).click();
  await card.getByRole('button', { name: 'Leave', exact: true }).click();
  await expect(cardOf(page, 'Potluck Lab')).toHaveCount(0);
  await expect(cardOf(page, 'Home lab')).toBeVisible();
  expect(coordinator.circles.map((c) => c.name)).toEqual(['Home lab']);
  expect(coordinator.calls).toContain(`DELETE /me/circles/${circle.id}/membership`);
});

test('an owner removes a person and sees the coordinator message when it fails', async ({ page }) => {
  const circle = ownedCircle();
  const coordinator = await openCircles(page, { circles: [circle] });
  const card = await openCard(page, 'Home lab');
  const people = card.getByRole('list', { name: 'People in this circle' });

  // First try fails: the row stays and the message says why.
  coordinator.failNext = { method: 'DELETE', path: /\/people\//, status: 404, detail: 'That person is not in this circle.' };
  await card.getByRole('button', { name: 'Remove Ada' }).click();
  await card.getByRole('button', { name: 'Remove Ada' }).click();
  await expect(card.getByRole('alert')).toHaveText('That person is not in this circle.');
  await expect(people).toContainText('Ada');

  // Second try: confirm, then Ada is gone and Grace is not.
  await card.getByRole('button', { name: 'Remove Ada' }).click();
  await card.getByRole('button', { name: 'Remove Ada' }).click();
  await expect(people).not.toContainText('Ada');
  await expect(people).toContainText('Grace');
  await expect(card).toContainText('2 people');
  expect(coordinator.calls).toContain(`DELETE /me/circles/${circle.id}/people/p-ada`);

  // Cancelling the confirm removes nobody.
  await card.getByRole('button', { name: 'Remove Grace' }).click();
  await card.getByRole('button', { name: 'Cancel remove' }).click();
  await expect(people).toContainText('Grace');
  expect(coordinator.calls).not.toContain(`DELETE /me/circles/${circle.id}/people/p-grace`);
});

test('an invalid invite shows the coordinator message and joins nothing', async ({ page }) => {
  const coordinator = await openCircles(page, { circles: [] });
  const join = page.getByRole('region', { name: 'Join a circle' });
  await page.getByRole('textbox', { name: 'Invite code or link' }).fill('NOPE-NOPE-NOPE');
  await page.getByRole('button', { name: 'Check invite' }).click();

  await expect(join.getByRole('alert')).toHaveText(INVALID_INVITE);
  await expect(page.getByRole('region', { name: 'Invite preview' })).toHaveCount(0);
  await expect(page.getByText('No circles yet.')).toBeVisible();
  expect(coordinator.calls).not.toContain('POST /circles/invites/accept');
  await expect(page.getByRole('button', { name: 'Check invite' })).toBeEnabled();

  // Editing the text clears the message.
  await page.getByRole('textbox', { name: 'Invite code or link' }).fill('NOPE-NOPE-NOPX');
  await expect(join.getByRole('alert')).toHaveCount(0);
});

test('a full circle is reported by the coordinator and is not added', async ({ page }) => {
  const circle = memberCircle();
  const coordinator = await openCircles(page, {
    circles: [],
    joinable: { 'FULL-CIRC-LE22': { invited_by: 'Ada', circle, full: true } },
  });
  await page.getByRole('textbox', { name: 'Invite code or link' }).fill('FULL-CIRC-LE22');
  await page.getByRole('button', { name: 'Check invite' }).click();
  await page.getByRole('button', { name: 'Join circle' }).click();

  await expect(page.getByRole('region', { name: 'Join a circle' }).getByRole('alert')).toHaveText('This circle is full.');
  await expect(page.getByText('No circles yet.')).toBeVisible();
  expect(coordinator.circles).toHaveLength(0);
  // The preview stays open so the choice can be cancelled.
  await expect(page.getByRole('button', { name: 'Join circle' })).toBeEnabled();
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByRole('region', { name: 'Invite preview' })).toHaveCount(0);
});

test('an invite to a circle the member is already in offers only Done', async ({ page }) => {
  const circle = memberCircle();
  const coordinator = await openCircles(page, {
    circles: [circle],
    joinable: { 'SAME-CIRC-LE33': { invited_by: 'Ada', circle } },
  });
  await page.getByRole('textbox', { name: 'Invite code or link' }).fill('same-circ-le33');
  await page.getByRole('button', { name: 'Check invite' }).click();

  const preview = page.getByRole('region', { name: 'Invite preview' });
  await expect(preview).toContainText('You are already in this circle.');
  await expect(preview).not.toContainText('Joining lets the people');
  await expect(preview.getByRole('button')).toHaveText(['Done']);
  await preview.getByRole('button', { name: 'Done' }).click();
  await expect(preview).toHaveCount(0);
  expect(coordinator.calls).not.toContain('POST /circles/invites/accept');
  expect(coordinator.circles).toHaveLength(1);
});

test('machine keys from an older version are listed only for circles that have them', async ({ page }) => {
  const withKeys = ownedCircle();
  withKeys.id = '44444444-4444-4444-8444-444444444444';
  withKeys.name = 'Old friends';
  withKeys.members = [{ member_pubkey: LEGACY_KEY, added_at: '2026-10-07T12:01:00Z' }];
  const coordinator = await openCircles(page, { circles: [ownedCircle(), withKeys, memberCircle()] });

  const clean = await openCard(page, 'Home lab');
  const old = await openCard(page, 'Old friends');
  await openCard(page, 'Potluck Lab');
  const heading = 'Machine keys added with an older version';
  await expect(page.getByRole('heading', { name: heading })).toHaveCount(1);
  await expect(clean.getByRole('heading', { name: heading })).toHaveCount(0);

  const keys = old.getByRole('list', { name: heading });
  await expect(keys.getByRole('listitem')).toHaveCount(1);
  await expect(keys).toContainText('ExampleDevic…AAAAAAAA');
  expect(coordinator.calls).not.toContain('GET /me/identity');

  await keys.getByRole('button', { name: /^Remove machine key/ }).click();
  await keys.getByRole('button', { name: /^Remove machine key/ }).click();
  await expect(old.getByRole('heading', { name: heading })).toHaveCount(0);
  expect(coordinator.calls).toContain(`DELETE /me/circles/${withKeys.id}/members/${LEGACY_KEY}`);
});

test('a circle from an older coordinator still shows as owned, with its keys', async ({ page }) => {
  // No role and no people in the response, as before invites existed.
  await openCircles(page);
  const card = await openCard(page, 'Friends');
  await expect(card).toContainText('You own this circle');
  await expect(card.getByRole('list', { name: 'People in this circle' })).toHaveCount(0);
  await expect(card.getByRole('list', { name: 'Machine keys added with an older version' })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Invite someone' })).toBeVisible();
});
