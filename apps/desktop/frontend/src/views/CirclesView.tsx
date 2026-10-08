/**
 * CirclesView — /circles route.
 *
 * A trusted circle is a named group of members who lend each other spare
 * compute. People get in by invite: the owner makes a one-time code, the
 * other person pastes it here, sees what they are joining, and chooses to
 * join. Nobody is added without being asked. This is Phase 2 of four-tier
 * routing.
 *
 * Layout:
 *   Header: "Trusted circles" with a live count, then one line pointing to
 *           the Network page, where a member turns sharing on.
 *   Join a circle: paste a code or link, check it, read what joining means,
 *                  then join or cancel. Nothing is joined on paste.
 *   Create a circle: name input + create button.
 *   Circle list: each card shows the caller's role. Expanded, it lists the
 *                people in the circle. The owner can remove people, make
 *                invites and withdraw open ones. A member can leave.
 *
 * Invite codes:
 *   The coordinator returns a code once, when the owner makes it. It lives
 *   only in the open card's state, so collapsing the card or reloading the
 *   page drops it for good. The open-invite list never carries codes.
 *
 * Legacy keys:
 *   Older app versions let an owner paste in machine keys. Those rows still
 *   exist on the coordinator, so the owner sees them in their own section and
 *   can remove them. Nothing here adds a key.
 *
 * Error handling policy:
 *   A failed load never shows one misleading catch-all message — it's
 *   classified (coordinatorError.ts) into what actually happened: signed
 *   out (401) gets a sign-in prompt, a coordinator misconfiguration (403)
 *   gets a diagnosable message, and unreachable/server-error get a calm
 *   empty state with a Retry button. None of these bubble past this view —
 *   the app shell (Sidebar/StatusBar) stays up regardless.
 *   A failed action shows the coordinator's own sentence next to the control
 *   that was used (circleErrorMessage).
 *   All destructive actions confirm inline (not a modal) following the
 *   HouseholdView pattern.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Users,
  Copy,
  Check,
  Plus,
  UserPlus,
  Trash2,
  Loader2,
  ChevronDown,
  ChevronRight,
  X,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type {
  Circle,
  CircleInvite,
  CircleInviteCreated,
  CircleInvitePreview,
  CircleMember,
  CirclePerson,
} from '@/lib/localApi';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import {
  classifyCoordinatorError,
  defaultCoordinatorErrorCopy,
  type CoordinatorFailureState,
} from '@/lib/coordinatorError';
import {
  canRemovePerson,
  circleErrorMessage,
  circleRoleLabel,
  cleanInviteInput,
  formatInviteDate,
  inviteUsesLabel,
  peopleCountLabel,
  shortKey,
  sinceLabel,
  upsertCircle,
  withoutKey,
  withoutPerson,
} from '@/lib/circles';

const primaryButtonClass = cn(
  'inline-flex h-9 items-center gap-1.5 rounded-lg px-4',
  'bg-primary text-sm font-medium text-primary-foreground',
  'hover:bg-primary/90 transition-colors',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
);

const secondaryButtonClass = cn(
  'inline-flex h-8 items-center gap-1.5 rounded-lg px-3',
  'border border-border/60 bg-card text-sm font-medium text-foreground',
  'hover:bg-accent transition-colors',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
  'disabled:cursor-not-allowed disabled:opacity-60',
);

const inlineLinkClass = cn(
  'rounded-sm underline underline-offset-2 hover:text-foreground',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
);

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export function CirclesView() {
  const [circles, setCircles] = useState<Circle[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorState, setErrorState] = useState<CoordinatorFailureState | null>(null);
  // A circle the owner just made opens, so the next step (inviting someone)
  // is already in view.
  const [createdId, setCreatedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const listRes = await localApi.circles.list();
      setCircles(listRes.circles);
      setErrorState(null);
    } catch (err) {
      setErrorState(classifyCoordinatorError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  // Runs after a change has already succeeded and been applied to the list on
  // screen, so a failure here keeps what is shown instead of replacing the
  // page with an error.
  const refresh = useCallback(async () => {
    try {
      const listRes = await localApi.circles.list();
      setCircles(listRes.circles);
    } catch {
      // The change went through; the list on screen already reflects it.
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!loading && errorState !== null) {
    // Signed out — this is not "the coordinator is down," it's "you need to
    // sign in." Distinct empty state, no error tone, wired to the app's
    // existing sign-in flow (the /membership route's magic-link form).
    if (errorState.kind === 'unauthenticated') {
      return (
        <div className="flex h-full flex-col">
          <CirclesHeader count={null} />
          <EmptyState
            icon={Users}
            title="Sign in to use circles"
            description="Trusted circles are tied to your account. Sign in to see and manage them."
            primaryAction={
              <Link to="/membership" className={primaryButtonClass}>
                Sign in
              </Link>
            }
          />
        </div>
      );
    }

    // Coordinator misconfiguration (403), unreachable, or a 5xx — calm
    // state with a Retry, not a wall. Copy is classified, not guessed.
    const copy = defaultCoordinatorErrorCopy(errorState);
    return (
      <div className="flex h-full flex-col">
        <CirclesHeader count={null} />
        <EmptyState
          icon={Users}
          tone={errorState.kind === 'forbidden' ? 'destructive' : 'warning'}
          title={copy.title}
          description={copy.description}
          primaryAction={
            <button type="button" onClick={() => void load()} className={primaryButtonClass}>
              Retry
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <CirclesHeader count={loading ? null : circles.length} />

      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-6">
        {/* Lending a machine is a Network setting; this page only says where. */}
        <p className="max-w-2xl text-xs text-muted-foreground">
          To let a circle use this machine, turn on Share spare compute under{' '}
          <Link to="/network" className={inlineLinkClass}>
            Network
          </Link>{' '}
          and choose My trusted circles too.
        </p>

        {/* Join with an invite */}
        <JoinCircleForm
          onJoined={async (joined) => {
            setCircles((prev) => upsertCircle(prev, joined));
            await refresh();
          }}
        />

        {/* Create a circle */}
        <CreateCircleForm
          onCreate={async (name) => {
            const created = await localApi.circles.create(name);
            setCircles((prev) => [...prev, created]);
            setCreatedId(created.id);
          }}
        />

        {/* Circle list */}
        <section aria-label="Your trusted circles">
          <SectionHeader>Your circles</SectionHeader>

          {loading ? (
            <div className="space-y-3">
              <SkeletonCard />
              <SkeletonCard />
            </div>
          ) : circles.length === 0 ? (
            <div className="rounded-xl border border-border/60 bg-card p-5 text-sm text-muted-foreground">
              No circles yet. Create one and invite someone, or join one with an invite code.
            </div>
          ) : (
            <ul className="space-y-3">
              {circles.map((circle) => (
                <CircleCard
                  key={circle.id}
                  circle={circle}
                  initiallyExpanded={circle.id === createdId}
                  onDeleted={() => {
                    setCircles((prev) => prev.filter((c) => c.id !== circle.id));
                  }}
                  onLeft={() => {
                    setCircles((prev) => prev.filter((c) => c.id !== circle.id));
                    void refresh();
                  }}
                  onPersonRemoved={(personId) => {
                    setCircles((prev) =>
                      prev.map((c) => (c.id === circle.id ? withoutPerson(c, personId) : c)),
                    );
                    void refresh();
                  }}
                  onKeyRemoved={(pubkey) => {
                    setCircles((prev) =>
                      prev.map((c) => (c.id === circle.id ? withoutKey(c, pubkey) : c)),
                    );
                  }}
                />
              ))}
            </ul>
          )}
        </section>

        {/* Privacy footer */}
        <p className="pt-2 text-[11px] text-muted-foreground/70 max-w-2xl">
          A trusted circle request goes through the Potluck coordinator to one
          machine in the circle and to no other machine. The member whose
          machine answers can see the conversation. Everyone in a circle can see
          who else is in it.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function CirclesHeader({ count }: { count: number | null }) {
  return (
    <ViewHeader
      eyebrow="Trusted routing"
      title="Trusted Circles"
      description={
        <span>
          Named groups of members you trust to run your prompts.
          {count !== null && count > 0 && (
            <> {count} circle{count === 1 ? '' : 's'} active.</>
          )}
        </span>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Join a circle
// ---------------------------------------------------------------------------

interface JoinCircleFormProps {
  /** Called after the coordinator accepted the join, with the circle now in the list. */
  onJoined: (circle: Circle) => Promise<void>;
}

const CONSENT_TEXT =
  'Joining lets the people in this circle send requests to your machines that share with trusted circles, and lets you send requests to theirs. The machine that answers sees the conversation. You can leave at any time.';

function JoinCircleForm({ onJoined }: JoinCircleFormProps) {
  const [input, setInput] = useState('');
  const [checking, setChecking] = useState(false);
  // The code that was checked travels with its preview, so what is joined is
  // what was shown even if the input is edited afterwards.
  const [preview, setPreview] = useState<{ code: string; data: CircleInvitePreview } | null>(null);
  const [joining, setJoining] = useState(false);
  const [joinedName, setJoinedName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Bring the preview into focus so a screen reader reads it and Tab lands on its buttons.
  useEffect(() => {
    if (preview !== null) panelRef.current?.focus();
  }, [preview]);

  function reset() {
    setInput('');
    setPreview(null);
    setError(null);
    inputRef.current?.focus();
  }

  async function handleCheck(e: React.FormEvent) {
    e.preventDefault();
    const code = cleanInviteInput(input);
    if (code.length === 0) return;
    setError(null);
    setJoinedName(null);
    setChecking(true);
    try {
      const data = await localApi.circles.previewInvite(code);
      setPreview({ code, data });
    } catch (err) {
      setError(circleErrorMessage(err, 'Could not check that invite.'));
    } finally {
      setChecking(false);
    }
  }

  async function handleJoin() {
    if (preview === null) return;
    setError(null);
    setJoining(true);
    try {
      const circle = await localApi.circles.acceptInvite(preview.code);
      await onJoined(circle);
      setJoinedName(circle.name);
      reset();
    } catch (err) {
      setError(circleErrorMessage(err, 'Could not join that circle.'));
    } finally {
      setJoining(false);
    }
  }

  return (
    <section aria-label="Join a circle">
      <SectionHeader>Join a circle</SectionHeader>
      <form onSubmit={(e) => void handleCheck(e)} className="flex items-start gap-2">
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            setError(null);
            setJoinedName(null);
          }}
          placeholder="Paste an invite code or link"
          aria-label="Invite code or link"
          autoComplete="off"
          spellCheck={false}
          readOnly={preview !== null || checking}
          className={cn(
            'flex-1 rounded-lg border border-input bg-background px-3 py-2',
            'font-mono text-xs text-foreground placeholder:font-sans placeholder:text-sm placeholder:text-muted-foreground/50',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'read-only:opacity-60',
          )}
        />
        <button
          type="submit"
          disabled={checking || preview !== null || cleanInviteInput(input).length === 0}
          className={cn(secondaryButtonClass, 'h-9 shrink-0')}
        >
          {checking && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          {checking ? 'Checking…' : 'Check invite'}
        </button>
      </form>

      {preview !== null && (
        <div
          ref={panelRef}
          role="region"
          aria-label="Invite preview"
          tabIndex={-1}
          className="mt-3 space-y-3 rounded-xl border border-primary/20 bg-card p-4 focus:outline-none"
        >
          <div className="space-y-0.5">
            <p className="text-sm font-medium text-foreground">{preview.data.circle_name}</p>
            <p className="text-xs text-muted-foreground">Invited by {preview.data.invited_by}</p>
            <p className="text-xs text-muted-foreground">
              {peopleCountLabel(preview.data.people)} in this circle
            </p>
          </div>

          {preview.data.already_in ? (
            <>
              <p className="text-sm text-foreground">You are already in this circle.</p>
              <div className="flex items-center gap-2">
                <button type="button" onClick={reset} className={secondaryButtonClass}>
                  Done
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="text-sm leading-relaxed text-foreground/90">{CONSENT_TEXT}</p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void handleJoin()}
                  disabled={joining}
                  className={cn(
                    primaryButtonClass,
                    'h-8 px-3.5 disabled:cursor-not-allowed disabled:opacity-60',
                  )}
                >
                  {joining && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
                  {joining ? 'Joining…' : 'Join circle'}
                </button>
                <button type="button" onClick={reset} disabled={joining} className={secondaryButtonClass}>
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {error !== null && (
        <p className="mt-2 text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
      {joinedName !== null && (
        <p className="mt-2 text-xs text-muted-foreground" role="status">
          You joined {joinedName}.
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Create circle form
// ---------------------------------------------------------------------------

interface CreateCircleFormProps {
  onCreate: (name: string) => Promise<void>;
}

function CreateCircleForm({ onCreate }: CreateCircleFormProps) {
  const [name, setName] = useState('');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (trimmed.length === 0) return;
    setError(null);
    setWorking(true);
    try {
      await onCreate(trimmed);
      setName('');
    } catch (err) {
      setError(circleErrorMessage(err, 'Could not create circle.'));
    } finally {
      setWorking(false);
    }
  }

  return (
    <section aria-label="Create a circle">
      <SectionHeader>Create a circle</SectionHeader>
      <form
        onSubmit={(e) => void handleSubmit(e)}
        className="flex items-start gap-2"
      >
        <div className="flex-1 space-y-1.5">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Circle name (e.g. Home lab)"
            maxLength={80}
            disabled={working}
            aria-label="New circle name"
            className={cn(
              'w-full rounded-lg border border-input bg-background px-3 py-2',
              'text-sm text-foreground placeholder:text-muted-foreground/50',
              'focus:outline-none focus:ring-2 focus:ring-ring',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          />
          {error !== null && (
            <p className="text-xs text-destructive" role="alert">
              {error}
            </p>
          )}
        </div>
        <button
          type="submit"
          disabled={working || name.trim().length === 0}
          className={cn(
            'inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg px-3.5',
            'bg-primary text-sm font-medium text-primary-foreground',
            'hover:bg-primary/90 transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          {working ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {working ? 'Creating…' : 'Create'}
        </button>
      </form>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Inline confirm focus
// ---------------------------------------------------------------------------

/**
 * Opening an inline confirm swaps the trigger button for the confirm buttons,
 * which drops keyboard focus. The confirm's Cancel button takes focus with
 * `autoFocus`, and this hook puts focus back on the trigger when the confirm
 * closes without acting.
 */
function useRestoreTriggerFocus() {
  const restore = useRef(false);
  const triggerRef = useCallback((el: HTMLElement | null) => {
    if (el !== null && restore.current) {
      restore.current = false;
      el.focus();
    }
  }, []);
  const restoreOnNextTrigger = useCallback(() => {
    restore.current = true;
  }, []);
  return { triggerRef, restoreOnNextTrigger };
}

// ---------------------------------------------------------------------------
// Circle card
// ---------------------------------------------------------------------------

interface CircleCardProps {
  circle: Circle;
  initiallyExpanded: boolean;
  onDeleted: () => void;
  onLeft: () => void;
  onPersonRemoved: (personId: string) => void;
  onKeyRemoved: (pubkey: string) => void;
}

function CircleCard({
  circle,
  initiallyExpanded,
  onDeleted,
  onLeft,
  onPersonRemoved,
  onKeyRemoved,
}: CircleCardProps) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const deleteFocus = useRestoreTriggerFocus();
  const leaveFocus = useRestoreTriggerFocus();
  const isOwner = circle.role === 'owner';

  async function handleDelete() {
    setActionError(null);
    setDeleting(true);
    try {
      await localApi.circles.delete(circle.id);
      onDeleted();
    } catch (err) {
      setActionError(circleErrorMessage(err, 'Could not delete circle.'));
      setDeleting(false);
      setConfirmDelete(false);
      deleteFocus.restoreOnNextTrigger();
    }
  }

  async function handleLeave() {
    setActionError(null);
    setLeaving(true);
    try {
      await localApi.circles.leave(circle.id);
      onLeft();
    } catch (err) {
      setActionError(circleErrorMessage(err, 'Could not leave circle.'));
      setLeaving(false);
      setConfirmLeave(false);
      leaveFocus.restoreOnNextTrigger();
    }
  }

  return (
    <li>
      <div
        className={cn(
          'overflow-hidden rounded-xl border border-border/60 bg-card',
          expanded && 'border-primary/20',
        )}
      >
        {/* Circle header row */}
        <div className="flex items-center gap-3 px-4 py-3">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className={cn(
              'flex flex-1 items-center gap-2.5 min-w-0 text-left',
              'focus-visible:outline-none',
            )}
            aria-expanded={expanded}
            aria-controls={`circle-body-${circle.id}`}
          >
            <Users
              className={cn(
                'h-4 w-4 shrink-0 transition-colors',
                expanded ? 'text-primary' : 'text-muted-foreground',
              )}
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1">
              <span className="truncate text-sm font-medium text-foreground">
                {circle.name}
              </span>
              <span className="ml-2 text-[11px] text-muted-foreground">
                {circleRoleLabel(circle.role)}
              </span>
              {circle.people.length > 0 && (
                <span className="ml-2 text-[11px] text-muted-foreground/70">
                  {peopleCountLabel(circle.people.length)}
                </span>
              )}
            </div>
            {expanded ? (
              <ChevronDown
                className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
            ) : (
              <ChevronRight
                className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
            )}
          </button>

          {isOwner ? (
            /* Delete control: inline confirm */
            !confirmDelete ? (
              <button
                ref={deleteFocus.triggerRef}
                type="button"
                onClick={() => setConfirmDelete(true)}
                aria-label={`Delete circle ${circle.name}`}
                className={cn(
                  'flex h-7 w-7 shrink-0 items-center justify-center rounded-md',
                  'text-muted-foreground hover:text-destructive hover:bg-destructive/10',
                  'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            ) : (
              <div className="flex items-center gap-1.5 shrink-0">
                {circle.people.length > 1 ? (
                  <span className="text-xs text-muted-foreground">Ends the circle for everyone in it.</span>
                ) : null}
                <button
                  type="button"
                  onClick={() => void handleDelete()}
                  disabled={deleting}
                  className={cn(
                    'inline-flex h-7 items-center rounded-md px-2 text-xs font-medium',
                    'bg-destructive text-destructive-foreground hover:bg-destructive/90',
                    'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    'disabled:cursor-not-allowed disabled:opacity-60',
                  )}
                >
                  {deleting ? (
                    <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                  ) : (
                    'Delete'
                  )}
                </button>
                <button
                  type="button"
                  autoFocus
                  onClick={() => {
                    setConfirmDelete(false);
                    deleteFocus.restoreOnNextTrigger();
                  }}
                  disabled={deleting}
                  aria-label="Cancel delete"
                  className={cn(
                    'flex h-7 w-7 items-center justify-center rounded-md',
                    'text-muted-foreground hover:bg-accent transition-colors',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  )}
                >
                  <X className="h-3 w-3" aria-hidden="true" />
                </button>
              </div>
            )
          ) : (
            !confirmLeave && (
              <button
                ref={leaveFocus.triggerRef}
                type="button"
                onClick={() => setConfirmLeave(true)}
                aria-label={`Leave circle ${circle.name}`}
                className={cn(
                  'inline-flex h-7 shrink-0 items-center rounded-md px-2 text-xs font-medium',
                  'text-muted-foreground hover:text-destructive hover:bg-destructive/10',
                  'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                Leave circle
              </button>
            )
          )}
        </div>

        {actionError !== null && (
          <p className="px-4 pb-2 text-xs text-destructive" role="alert">
            {actionError}
          </p>
        )}

        {/* Leave confirm: says what leaving changes for both sides */}
        {!isOwner && confirmLeave && (
          <div className="space-y-2.5 border-t border-border/40 px-4 py-3">
            <p className="text-xs text-foreground/90">
              People in this circle will no longer be able to use your machines, and you will
              no longer be able to use theirs.
            </p>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => void handleLeave()}
                disabled={leaving}
                className={cn(
                  'inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-xs font-medium',
                  'bg-destructive text-destructive-foreground hover:bg-destructive/90',
                  'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  'disabled:cursor-not-allowed disabled:opacity-60',
                )}
              >
                {leaving && <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />}
                Leave
              </button>
              <button
                type="button"
                autoFocus
                onClick={() => {
                  setConfirmLeave(false);
                  leaveFocus.restoreOnNextTrigger();
                }}
                disabled={leaving}
                aria-label="Cancel leave"
                className={cn(
                  'flex h-7 w-7 items-center justify-center rounded-md',
                  'text-muted-foreground hover:bg-accent transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                <X className="h-3 w-3" aria-hidden="true" />
              </button>
            </div>
          </div>
        )}

        {/* Expanded body */}
        {expanded && (
          <div
            id={`circle-body-${circle.id}`}
            className="border-t border-border/40 px-4 py-4 space-y-5"
          >
            {circle.people.length > 0 && (
              <div>
                <CardLabel>People</CardLabel>
                <ul className="space-y-1.5" aria-label="People in this circle">
                  {circle.people.map((person) => (
                    <PersonRow
                      key={person.id}
                      person={person}
                      circle={circle}
                      onRemoved={() => onPersonRemoved(person.id)}
                    />
                  ))}
                </ul>
              </div>
            )}

            {isOwner && <InviteSection circleId={circle.id} />}

            {isOwner && circle.members.length > 0 && (
              <div>
                <CardLabel>Machine keys added with an older version</CardLabel>
                <ul
                  className="space-y-1.5"
                  aria-label="Machine keys added with an older version"
                >
                  {circle.members.map((member) => (
                    <LegacyKeyRow
                      key={member.member_pubkey}
                      member={member}
                      circleId={circle.id}
                      onRemoved={() => onKeyRemoved(member.member_pubkey)}
                    />
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// People
// ---------------------------------------------------------------------------

function PersonRow({
  person,
  circle,
  onRemoved,
}: {
  person: CirclePerson;
  circle: Circle;
  onRemoved: () => void;
}) {
  return (
    <li className="flex items-center gap-2.5">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <span className="truncate text-sm text-foreground">{person.name}</span>
        {person.is_you && <span className="shrink-0 text-xs text-muted-foreground">(you)</span>}
        {person.role === 'owner' && (
          <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            Owner
          </span>
        )}
        <span className="shrink-0 text-[10px] text-muted-foreground/70">
          joined {sinceLabel(person.joined_at)}
        </span>
      </div>

      {canRemovePerson(circle, person) && (
        <InlineRemove
          label={`Remove ${person.name}`}
          failure="Could not remove that person."
          onRemove={async () => {
            await localApi.circles.removePerson(circle.id, person.id);
            onRemoved();
          }}
        />
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Invites (owner)
// ---------------------------------------------------------------------------

/**
 * Lives inside the expanded card body, so collapsing the card unmounts it and
 * drops the one-time code held in `fresh`. The coordinator will not return the
 * code again.
 */
function InviteSection({ circleId }: { circleId: string }) {
  const [invites, setInvites] = useState<CircleInvite[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [fresh, setFresh] = useState<CircleInviteCreated | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const loadInvites = useCallback(async () => {
    try {
      setInvites(await localApi.circles.listInvites(circleId));
      setListError(null);
    } catch (err) {
      setListError(circleErrorMessage(err, 'Could not load open invites.'));
    }
  }, [circleId]);

  useEffect(() => {
    void loadInvites();
  }, [loadInvites]);

  async function handleCreate() {
    setCreateError(null);
    setCreating(true);
    try {
      setFresh(await localApi.circles.createInvite(circleId));
      void loadInvites();
    } catch (err) {
      setCreateError(circleErrorMessage(err, 'Could not create an invite.'));
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <button
          type="button"
          onClick={() => void handleCreate()}
          disabled={creating}
          className={secondaryButtonClass}
        >
          {creating ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {creating ? 'Making invite…' : 'Invite someone'}
        </button>
        {createError !== null && (
          <p className="text-xs text-destructive" role="alert">
            {createError}
          </p>
        )}
      </div>

      {fresh !== null && <FreshInvite circleId={circleId} invite={fresh} />}

      <div>
        <CardLabel>Open invites</CardLabel>
        {listError !== null ? (
          <p className="text-xs text-destructive" role="alert">
            {listError}
          </p>
        ) : invites === null ? (
          <p className="text-xs text-muted-foreground">Loading invites…</p>
        ) : invites.length === 0 ? (
          <p className="text-xs text-muted-foreground">No open invites.</p>
        ) : (
          <ul className="space-y-1.5" aria-label="Open invites">
            {invites.map((invite) => (
              <InviteRow
                key={invite.id}
                circleId={circleId}
                invite={invite}
                onWithdrawn={() => setInvites((prev) => prev?.filter((i) => i.id !== invite.id) ?? null)}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function FreshInvite({ circleId, invite }: { circleId: string; invite: CircleInviteCreated }) {
  return (
    <div
      role="group"
      aria-label="New invite"
      className="space-y-3 rounded-lg border border-primary/20 bg-background/60 p-3"
    >
      <div className="space-y-1">
        <label
          htmlFor={`invite-code-${circleId}`}
          className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label"
        >
          Invite code
        </label>
        <input
          id={`invite-code-${circleId}`}
          type="text"
          readOnly
          value={invite.code}
          onFocus={(e) => e.currentTarget.select()}
          spellCheck={false}
          className={cn(
            'w-full rounded-lg border border-border/40 bg-background px-3 py-2.5',
            'font-mono text-xl tracking-[0.18em] text-foreground',
            'focus:outline-none focus:ring-2 focus:ring-ring',
          )}
        />
      </div>

      <div className="space-y-1">
        <label
          htmlFor={`invite-link-${circleId}`}
          className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label"
        >
          Invite link
        </label>
        <input
          id={`invite-link-${circleId}`}
          type="text"
          readOnly
          value={invite.url}
          onFocus={(e) => e.currentTarget.select()}
          spellCheck={false}
          className={cn(
            'w-full rounded-lg border border-border/40 bg-background px-3 py-2',
            'font-mono text-[11px] text-foreground/85',
            'focus:outline-none focus:ring-2 focus:ring-ring',
          )}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <CopyButton label="Copy code" value={invite.code} />
        <CopyButton label="Copy link" value={invite.url} />
        <span className="text-xs text-muted-foreground">
          Expires {formatInviteDate(invite.expires_at)}
        </span>
      </div>

      <p className="text-xs text-muted-foreground">
        This code works once and is shown only now. Send it to one person you trust.
      </p>
    </div>
  );
}

function CopyButton({ label, value }: { label: string; value: string }) {
  const [outcome, setOutcome] = useState<'copied' | 'failed' | null>(null);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(value);
      setOutcome('copied');
    } catch {
      // The field above stays selectable, so the text can still be copied by hand.
      setOutcome('failed');
    }
    setTimeout(() => setOutcome(null), 2000);
  }

  return (
    <button type="button" onClick={() => void handleCopy()} className={secondaryButtonClass}>
      {outcome === 'copied' ? (
        <>
          <Check className="h-3.5 w-3.5 text-sage" aria-hidden="true" />
          Copied
        </>
      ) : outcome === 'failed' ? (
        'Copy failed'
      ) : (
        <>
          <Copy className="h-3.5 w-3.5" aria-hidden="true" />
          {label}
        </>
      )}
    </button>
  );
}

function InviteRow({
  circleId,
  invite,
  onWithdrawn,
}: {
  circleId: string;
  invite: CircleInvite;
  onWithdrawn: () => void;
}) {
  const [withdrawing, setWithdrawing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const describedBy = `invite-${invite.id}`;

  async function handleWithdraw() {
    setError(null);
    setWithdrawing(true);
    try {
      await localApi.circles.revokeInvite(circleId, invite.id);
      onWithdrawn();
    } catch (err) {
      setError(circleErrorMessage(err, 'Could not withdraw that invite.'));
      setWithdrawing(false);
    }
  }

  return (
    <li className="flex items-center gap-2.5">
      <span id={describedBy} className="min-w-0 flex-1 text-xs text-muted-foreground">
        Created {sinceLabel(invite.created_at)}. Expires {formatInviteDate(invite.expires_at)}.{' '}
        {inviteUsesLabel(invite.uses, invite.max_uses)}.
      </span>

      {error !== null && (
        <span className="text-[10px] text-destructive" role="alert">
          {error}
        </span>
      )}

      <button
        type="button"
        onClick={() => void handleWithdraw()}
        disabled={withdrawing}
        aria-describedby={describedBy}
        className={cn(secondaryButtonClass, 'h-7 px-2 text-xs')}
      >
        {withdrawing && <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />}
        Withdraw
      </button>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Legacy machine keys
// ---------------------------------------------------------------------------

function LegacyKeyRow({
  member,
  circleId,
  onRemoved,
}: {
  member: CircleMember;
  circleId: string;
  onRemoved: () => void;
}) {
  const short = shortKey(member.member_pubkey);

  return (
    <li className="flex items-center gap-2.5">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {/* Truncated for readability; the full key is in the title. */}
        <code className="truncate font-mono text-[11px] text-foreground/80" title={member.member_pubkey}>
          {short}
        </code>
        <span className="shrink-0 text-[10px] text-muted-foreground/70">
          added {sinceLabel(member.added_at)}
        </span>
      </div>

      <InlineRemove
        label={`Remove machine key ${short}`}
        failure="Could not remove that key."
        onRemove={async () => {
          await localApi.circles.removeMember(circleId, member.member_pubkey);
          onRemoved();
        }}
      />
    </li>
  );
}

// ---------------------------------------------------------------------------
// Inline remove: X button, then Remove / Cancel
// ---------------------------------------------------------------------------

/**
 * `onRemove` resolves once the coordinator has removed the row; the parent
 * then drops the row, which unmounts this control. A failure keeps the row,
 * closes the confirm and shows the coordinator's message beside it.
 */
function InlineRemove({
  label,
  failure,
  onRemove,
}: {
  label: string;
  failure: string;
  onRemove: () => Promise<void>;
}) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const focus = useRestoreTriggerFocus();

  async function handleRemove() {
    setError(null);
    setRemoving(true);
    try {
      await onRemove();
    } catch (err) {
      setError(circleErrorMessage(err, failure));
      setRemoving(false);
      setConfirmRemove(false);
      focus.restoreOnNextTrigger();
    }
  }

  return (
    <>
      {error !== null && (
        <span className="text-[10px] text-destructive" role="alert">
          {error}
        </span>
      )}

      {!confirmRemove ? (
        <button
          ref={focus.triggerRef}
          type="button"
          onClick={() => setConfirmRemove(true)}
          aria-label={label}
          className={cn(
            'flex h-6 w-6 shrink-0 items-center justify-center rounded',
            'text-muted-foreground/60 hover:text-destructive hover:bg-destructive/10',
            'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <X className="h-3 w-3" aria-hidden="true" />
        </button>
      ) : (
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={() => void handleRemove()}
            disabled={removing}
            aria-label={label}
            className={cn(
              'inline-flex h-6 items-center rounded px-1.5 text-[10px] font-medium',
              'bg-destructive text-destructive-foreground hover:bg-destructive/90',
              'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            {removing ? <Loader2 className="h-2.5 w-2.5 animate-spin" aria-hidden="true" /> : 'Remove'}
          </button>
          <button
            type="button"
            autoFocus
            onClick={() => {
              setConfirmRemove(false);
              focus.restoreOnNextTrigger();
            }}
            disabled={removing}
            aria-label="Cancel remove"
            className={cn(
              'flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-accent',
              'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <X className="h-2.5 w-2.5" aria-hidden="true" />
          </button>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Section header — matches NetworkView / HouseholdView pattern
// ---------------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
      {children}
    </h2>
  );
}

/** Same voice as SectionHeader, one level down, for the parts inside an open card. */
function CardLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 font-serif text-[11px] italic text-muted-foreground tracking-hearth-label">
      {children}
    </h3>
  );
}

// ---------------------------------------------------------------------------
// Skeleton card (loading state)
// ---------------------------------------------------------------------------

function SkeletonCard() {
  return (
    <div
      className="rounded-xl border border-border/60 bg-card px-4 py-3 animate-pulse"
      aria-busy="true"
    >
      <div className="flex items-center gap-2.5">
        <div className="h-4 w-4 rounded bg-muted" />
        <div className="h-4 w-32 rounded bg-muted" />
        <div className="h-3 w-16 rounded bg-muted ml-2" />
      </div>
    </div>
  );
}
