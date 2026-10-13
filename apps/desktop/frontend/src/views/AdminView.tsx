/**
 * AdminView - /admin route. Member operations for Potluck administrators.
 *
 * Today this is the member roster with grant/revoke founding-member status.
 * Every mutation is written to the audit_log server-side (see routers/admin.py).
 * Billing/MRR and pagination controls come in a later pass.
 *
 * Access: gated on the signed-in member's isAdmin flag. The API is the real
 * gate (403 for non-admins); this just avoids showing a wall of errors.
 */

import { useCallback, useState } from 'react';
import { ShieldAlert, RefreshCw } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ViewHeader } from '@/components/layout/ViewHeader';
import { EmptyState } from '@/components/layout/EmptyState';
import { useAuth } from '@/hooks/useAuth';
import { useAdminMembers } from '@/hooks/useAdminMembers';
import type { AdminMemberSummary } from '@/types/api';

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

function Pill({
  tone,
  children,
}: {
  tone: 'sage' | 'primary';
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-medium',
        tone === 'sage'
          ? 'border-sage/25 bg-sage/12 text-sage'
          : 'border-primary/25 bg-primary/12 text-primary',
      )}
    >
      {children}
    </span>
  );
}

export function AdminView() {
  const { memberProfile } = useAuth();
  const isAdmin = memberProfile?.isAdmin ?? false;

  const { page, isLoading, error, refresh, grantFounding, revokeFounding } =
    useAdminMembers();

  // Per-row in-flight guard so only the toggled row's button disables.
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleToggleFounding = useCallback(
    async (member: AdminMemberSummary) => {
      setActionError(null);
      setBusyId(member.id);
      try {
        if (member.isFoundingMember) {
          await revokeFounding(member.id);
        } else {
          await grantFounding(member.id);
        }
      } catch (err) {
        setActionError(err instanceof Error ? err.message : 'Action failed');
      } finally {
        setBusyId(null);
      }
    },
    [grantFounding, revokeFounding],
  );

  // ---- Access gate ---------------------------------------------------------
  if (!isAdmin) {
    return (
      <div className="flex h-full flex-col">
        <ViewHeader title="Admin" eyebrow="Operations" />
        <div className="flex flex-1 overflow-y-auto">
          <EmptyState
            icon={ShieldAlert}
            tone="warning"
            title="Admins only"
            description="This area is for Potluck administrators. Your account does not have admin access."
          />
        </div>
      </div>
    );
  }

  const members = page?.members ?? [];

  return (
    <div className="flex h-full flex-col">
      <ViewHeader
        title="Members"
        eyebrow="Operations"
        count={page?.total ?? null}
        description="Grant or revoke founding-member status. Every change is recorded in the audit log."
        actions={
          <button
            type="button"
            onClick={() => refresh()}
            disabled={isLoading}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5',
              'text-sm font-medium text-muted-foreground transition-colors',
              'hover:bg-muted/50 hover:text-foreground disabled:opacity-50',
            )}
          >
            <RefreshCw className={cn('h-3.5 w-3.5', isLoading && 'animate-spin')} />
            Refresh
          </button>
        }
      />

      <div className="flex-1 overflow-y-auto">
        {/* Loading (first load only) */}
        {isLoading && !page ? (
          <div className="flex h-full items-center justify-center">
            <div
              className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent"
              role="status"
              aria-label="Loading members"
            />
          </div>
        ) : error && !page ? (
          <EmptyState
            icon={ShieldAlert}
            tone="destructive"
            title="Could not load members"
            description={error}
            primaryAction={
              <button
                type="button"
                onClick={() => refresh()}
                className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Try again
              </button>
            }
          />
        ) : members.length === 0 ? (
          <EmptyState
            title="No members yet"
            description="Members will appear here as people sign in."
          />
        ) : (
          <div className="p-6">
            {actionError && (
              <p role="alert" className="mb-3 text-xs text-destructive">
                {actionError}
              </p>
            )}

            <div className="overflow-x-auto rounded-xl border border-border bg-card">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="px-4 py-3 font-medium">Member</th>
                    <th className="px-4 py-3 font-medium">Membership</th>
                    <th className="px-4 py-3 font-medium">Badges</th>
                    <th className="px-4 py-3 font-medium whitespace-nowrap">Joined</th>
                    <th className="px-4 py-3 font-medium text-right">Founding</th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => {
                    const busy = busyId === m.id;
                    return (
                      <tr
                        key={m.id}
                        className="border-b border-border/60 transition-colors last:border-0 hover:bg-muted/30"
                      >
                        <td className="px-4 py-3">
                          <div className="font-medium text-foreground">{m.email}</div>
                          {m.displayName && (
                            <div className="text-xs text-muted-foreground">
                              {m.displayName}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {m.membershipPlan
                            ? `${titleCase(m.membershipPlan)}${
                                m.membershipStatus ? ` · ${titleCase(m.membershipStatus)}` : ''
                              }`
                            : 'None'}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-1.5">
                            {m.isFoundingMember && <Pill tone="sage">Founding</Pill>}
                            {m.isAdmin && <Pill tone="primary">Admin</Pill>}
                            {!m.isFoundingMember && !m.isAdmin && (
                              <span className="text-xs text-muted-foreground">None</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-muted-foreground whitespace-nowrap">
                          {formatDate(m.joinedAt)}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            type="button"
                            onClick={() => void handleToggleFounding(m)}
                            disabled={busy}
                            className={cn(
                              'inline-flex min-w-[7.5rem] items-center justify-center rounded-lg px-3 py-1.5',
                              'text-xs font-medium transition-colors disabled:opacity-50',
                              m.isFoundingMember
                                ? 'border border-border text-muted-foreground hover:bg-muted/50 hover:text-foreground'
                                : 'bg-primary text-primary-foreground hover:bg-primary/90',
                            )}
                          >
                            {busy
                              ? 'Working…'
                              : m.isFoundingMember
                                ? 'Revoke founding'
                                : 'Grant founding'}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {page && page.total > members.length && (
              <p className="mt-3 text-xs text-muted-foreground">
                Showing {members.length} of {page.total.toLocaleString()}. Pagination
                controls come in a later pass.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
