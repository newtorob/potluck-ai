/**
 * CirclesView — /circles route.
 *
 * Trusted circles let you route chat to a named group of members whose
 * public keys you have gathered and trust. This is Phase 2 of four-tier
 * routing.
 *
 * Layout:
 *   Header — "Trusted circles" with a live count.
 *   Your key — collapsible card showing the member's own pubkey with a
 *               copy button, so they can share it with others.
 *   Circle list — each circle card expands to show its members. Per-member
 *                 remove button. Delete-circle confirm inline.
 *   Create circle — name input + create button, top of the list area.
 *
 * Copy/paste design notes:
 *   Keys are long hex strings. We show the full key in a scrollable mono
 *   box rather than truncating, because truncation is confusing when the
 *   user needs to copy an exact string.
 *
 * Error handling policy:
 *   A failed load never shows one misleading catch-all message — it's
 *   classified (coordinatorError.ts) into what actually happened: signed
 *   out (401) gets a sign-in prompt, a coordinator misconfiguration (403)
 *   gets a diagnosable message, and unreachable/server-error get a calm
 *   empty state with a Retry button. None of these bubble past this view —
 *   the app shell (Sidebar/StatusBar) stays up regardless.
 *   Duplicate-member 409 = inline "already a member" note.
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
  Trash2,
  Loader2,
  KeyRound,
  ChevronDown,
  ChevronRight,
  X,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type { Circle, CircleMember } from '@/lib/localApi';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import { relativeTime } from '@/lib/timeFormat';
import {
  classifyCoordinatorError,
  defaultCoordinatorErrorCopy,
  type CoordinatorFailureState,
} from '@/lib/coordinatorError';

const primaryButtonClass = cn(
  'inline-flex h-9 items-center gap-1.5 rounded-lg px-4',
  'bg-primary text-sm font-medium text-primary-foreground',
  'hover:bg-primary/90 transition-colors',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
);

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export function CirclesView() {
  const [circles, setCircles] = useState<Circle[]>([]);
  const [myKey, setMyKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [errorState, setErrorState] = useState<CoordinatorFailureState | null>(null);

  // Note: `list()` failing is the failure this view classifies and renders.
  // `getMyIdentity()` failing is swallowed (`.catch(() => null)`) — the key
  // card already has its own "didn't return an identity" fallback below, so
  // one call's failure doesn't need to block the whole view.
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [listRes, identityRes] = await Promise.all([
        localApi.circles.list(),
        localApi.getMyIdentity().catch(() => null),
      ]);
      setCircles(listRes.circles);
      setMyKey(identityRes?.member_pubkey ?? null);
      setErrorState(null);
    } catch (err) {
      setErrorState(classifyCoordinatorError(err));
    } finally {
      setLoading(false);
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
        {/* Your public key — share this so others can add you to their circles. */}
        <YourKeyCard myKey={myKey} loading={loading} />

        {/* Create a circle */}
        <CreateCircleForm
          onCreate={async (name) => {
            const created = await localApi.circles.create(name);
            setCircles((prev) => [...prev, created]);
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
              No circles yet. Create one to route to a group of members you trust.
            </div>
          ) : (
            <ul className="space-y-3">
              {circles.map((circle) => (
                <CircleCard
                  key={circle.id}
                  circle={circle}
                  onDeleted={() => {
                    setCircles((prev) => prev.filter((c) => c.id !== circle.id));
                  }}
                  onMemberAdded={(member) => {
                    setCircles((prev) =>
                      prev.map((c) =>
                        c.id === circle.id
                          ? { ...c, members: [...c.members, member] }
                          : c,
                      ),
                    );
                  }}
                  onMemberRemoved={(pubkey) => {
                    setCircles((prev) =>
                      prev.map((c) =>
                        c.id === circle.id
                          ? { ...c, members: c.members.filter((m) => m.member_pubkey !== pubkey) }
                          : c,
                      ),
                    );
                  }}
                />
              ))}
            </ul>
          )}
        </section>

        {/* Privacy footer */}
        <p className="pt-2 text-[11px] text-muted-foreground/70 max-w-2xl">
          When you route a chat through a trusted circle, the Potluck
          coordinator passes that conversation to one machine in the circle and
          to no other machine. The member whose machine answers can see it.
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
// Your key card
// ---------------------------------------------------------------------------

function YourKeyCard({ myKey, loading }: { myKey: string | null; loading: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    if (!myKey) return;
    await navigator.clipboard.writeText(myKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <section aria-label="Your public key">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          'flex w-full items-center justify-between gap-3',
          'rounded-xl border border-border/60 bg-card px-4 py-3',
          'text-left transition-colors hover:bg-accent/40',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        )}
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2.5">
          <KeyRound className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="text-sm font-medium text-foreground">Your key</span>
          <span className="text-[11px] text-muted-foreground">
            Share this so others can add you to a circle
          </span>
        </div>
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        )}
      </button>

      {expanded && (
        <div className="mt-2 rounded-xl border border-border/60 bg-card p-4 space-y-3">
          {loading ? (
            <div className="h-4 w-48 animate-pulse rounded bg-muted" />
          ) : myKey !== null ? (
            <>
              <div
                className={cn(
                  'rounded-lg border border-border/40 bg-background/60 px-3 py-2.5',
                  'overflow-x-auto',
                )}
              >
                <code className="break-all font-mono text-[11px] text-foreground/85 select-all">
                  {myKey}
                </code>
              </div>
              <button
                type="button"
                onClick={() => void handleCopy()}
                className={cn(
                  'inline-flex h-8 items-center gap-1.5 rounded-lg px-3',
                  'border border-border/60 bg-card text-sm font-medium text-foreground',
                  'hover:bg-accent transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                )}
              >
                {copied ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-sage" aria-hidden="true" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" aria-hidden="true" />
                    Copy key
                  </>
                )}
              </button>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              Coordinator didn't return an identity. Check the connection.
            </p>
          )}
        </div>
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
      setError(err instanceof Error ? err.message : 'Could not create circle.');
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
// Circle card
// ---------------------------------------------------------------------------

interface CircleCardProps {
  circle: Circle;
  onDeleted: () => void;
  onMemberAdded: (member: CircleMember) => void;
  onMemberRemoved: (pubkey: string) => void;
}

function CircleCard({ circle, onDeleted, onMemberAdded, onMemberRemoved }: CircleCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDelete() {
    setDeleteError(null);
    setDeleting(true);
    try {
      await localApi.circles.delete(circle.id);
      onDeleted();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Could not delete circle.');
      setDeleting(false);
      setConfirmDelete(false);
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
                {circle.members.length} member{circle.members.length === 1 ? '' : 's'}
              </span>
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

          {/* Delete control — inline confirm */}
          {!confirmDelete ? (
            <button
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
                onClick={() => setConfirmDelete(false)}
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
          )}
        </div>

        {deleteError !== null && (
          <p className="px-4 pb-2 text-xs text-destructive" role="alert">
            {deleteError}
          </p>
        )}

        {/* Expanded body */}
        {expanded && (
          <div
            id={`circle-body-${circle.id}`}
            className="border-t border-border/40 px-4 py-4 space-y-4"
          >
            {/* Member list */}
            {circle.members.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No members yet. Add a member by their public key below.
              </p>
            ) : (
              <ul className="space-y-1.5" aria-label="Circle members">
                {circle.members.map((member) => (
                  <MemberRow
                    key={member.member_pubkey}
                    member={member}
                    circleId={circle.id}
                    onRemoved={() => onMemberRemoved(member.member_pubkey)}
                  />
                ))}
              </ul>
            )}

            {/* Add member form */}
            <AddMemberForm
              circleId={circle.id}
              existingKeys={new Set(circle.members.map((m) => m.member_pubkey))}
              onAdded={onMemberAdded}
            />
          </div>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Member row
// ---------------------------------------------------------------------------

function MemberRow({
  member,
  circleId,
  onRemoved,
}: {
  member: CircleMember;
  circleId: string;
  onRemoved: () => void;
}) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRemove() {
    setError(null);
    setRemoving(true);
    try {
      await localApi.circles.removeMember(circleId, member.member_pubkey);
      onRemoved();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove member.');
      setRemoving(false);
      setConfirmRemove(false);
    }
  }

  // Show truncated key for readability; full key in title for accessibility.
  const shortKey =
    member.member_pubkey.length > 24
      ? `${member.member_pubkey.slice(0, 12)}…${member.member_pubkey.slice(-8)}`
      : member.member_pubkey;

  return (
    <li className="flex items-center gap-2.5">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <code
          className="truncate font-mono text-[11px] text-foreground/80"
          title={member.member_pubkey}
        >
          {shortKey}
        </code>
        <span className="shrink-0 text-[10px] text-muted-foreground/70">
          added {relativeTime(member.added_at)}
        </span>
      </div>

      {error !== null && (
        <span className="text-[10px] text-destructive" role="alert">
          {error}
        </span>
      )}

      {!confirmRemove ? (
        <button
          type="button"
          onClick={() => setConfirmRemove(true)}
          aria-label="Remove member"
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
            className={cn(
              'inline-flex h-6 items-center rounded px-1.5 text-[10px] font-medium',
              'bg-destructive text-destructive-foreground hover:bg-destructive/90',
              'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            {removing ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : 'Remove'}
          </button>
          <button
            type="button"
            onClick={() => setConfirmRemove(false)}
            disabled={removing}
            aria-label="Cancel remove"
            className={cn(
              'flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-accent',
              'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <X className="h-2.5 w-2.5" />
          </button>
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Add member form
// ---------------------------------------------------------------------------

interface AddMemberFormProps {
  circleId: string;
  existingKeys: Set<string>;
  onAdded: (member: CircleMember) => void;
}

function AddMemberForm({ circleId, existingKeys, onAdded }: AddMemberFormProps) {
  const [pubkey, setPubkey] = useState('');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = pubkey.trim();
    if (trimmed.length === 0) return;
    if (existingKeys.has(trimmed)) {
      setError('That key is already a member of this circle.');
      return;
    }
    setError(null);
    setWorking(true);
    try {
      const member = await localApi.circles.addMember(circleId, trimmed);
      onAdded(member);
      setPubkey('');
      inputRef.current?.focus();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Could not add member.';
      // 409 from the coordinator means duplicate; surface it calmly.
      if (msg.includes('409')) {
        setError('Already a member of this circle.');
      } else {
        setError(msg);
      }
    } finally {
      setWorking(false);
    }
  }

  return (
    <form
      onSubmit={(e) => void handleSubmit(e)}
      className="space-y-1.5"
      aria-label="Add member by public key"
    >
      <label
        htmlFor={`add-member-${circleId}`}
        className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label"
      >
        Add a member
      </label>
      <div className="flex gap-2">
        <input
          ref={inputRef}
          id={`add-member-${circleId}`}
          type="text"
          value={pubkey}
          onChange={(e) => { setPubkey(e.target.value); setError(null); }}
          placeholder="Paste their public key…"
          autoComplete="off"
          spellCheck={false}
          disabled={working}
          className={cn(
            'flex-1 rounded-lg border border-input bg-background px-3 py-2',
            'font-mono text-[11px] text-foreground placeholder:text-muted-foreground/50',
            'focus:outline-none focus:ring-2 focus:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        />
        <button
          type="submit"
          disabled={working || pubkey.trim().length === 0}
          className={cn(
            'inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg px-3',
            'border border-border/60 bg-card text-sm font-medium text-foreground',
            'hover:bg-accent transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          {working ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          Add
        </button>
      </div>
      {error !== null && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </form>
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
