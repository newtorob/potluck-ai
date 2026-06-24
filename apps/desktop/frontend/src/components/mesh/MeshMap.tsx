/**
 * MeshMap — a Deco-style radial map of the household mesh: a central
 * "Household" hub with each machine (this one + peers) placed around it, the
 * spokes coloured by each machine's live connection state.
 *
 * Deliberately a hand-laid radial layout, not a physics force-graph: a
 * household is a handful of nodes, and a static ring reads cleaner (and matches
 * the mental model of a router app like TP-Link Deco) than floating nodes.
 *
 * Single source of truth: it consumes the SAME deriveDiagnostics() report the
 * roster list uses, so a machine's colour here always matches its status pill
 * below. No new probing, no parallel state.
 *
 * Privacy: renders only data already on screen in the roster (names, mesh IPs,
 * connection state). No new network calls.
 */

import { Home, Monitor } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { MeshPeer, MeshStatus } from '@/types/mesh';
import type { DiagnosticsReport, PeerState } from '@/lib/meshDiagnostics';

/** Per-state colour (Tailwind text-* so SVG strokes + dots use currentColor),
 *  mirroring the roster's PeerStateBadge palette. */
const STATE_COLOR: Record<PeerState, string> = {
  direct: 'text-sage',
  relayed: 'text-amber-500',
  connecting: 'text-muted-foreground',
  no_data_plane: 'text-destructive',
  key_drift: 'text-amber-500',
  status_unknown: 'text-muted-foreground/50',
};

/** Direct links are solid; everything else is dashed (degraded or coming up). */
function isSolid(state: PeerState): boolean {
  return state === 'direct';
}

interface MapNode {
  id: string;
  name: string;
  ip: string | null;
  isSelf: boolean;
  state: PeerState;
  label: string;
  detail: string;
}

/** Map this machine's coordinator-connection into the shared PeerState palette. */
function selfState(status: MeshStatus): PeerState {
  if (status.mesh_status === 'error') return 'no_data_plane';
  switch (status.coordinator_connection) {
    case 'connected':
      return 'direct';
    case 'connecting':
      return 'connecting';
    default:
      return 'no_data_plane';
  }
}

function selfLabel(status: MeshStatus): string {
  switch (status.coordinator_connection) {
    case 'connected':
      return 'Connected';
    case 'connecting':
      return 'Reconnecting';
    default:
      return 'Offline';
  }
}

export function MeshMap({
  status,
  peers,
  report,
}: {
  status: MeshStatus;
  peers: MeshPeer[];
  report: DiagnosticsReport;
}) {
  const nodes: MapNode[] = [
    {
      id: status.node_id ?? 'self',
      name: 'This machine',
      ip: status.mesh_ip,
      isSelf: true,
      state: selfState(status),
      label: selfLabel(status),
      detail: `This machine (${status.mesh_ip ?? 'no mesh IP'}) — ${selfLabel(status)} to the coordinator`,
    },
    ...peers.map((peer): MapNode => {
      const d = report.perPeer[peer.node_id];
      const state = d?.state ?? 'status_unknown';
      return {
        id: peer.node_id,
        name: peer.node_name,
        ip: peer.mesh_ip,
        isSelf: false,
        state,
        label: d?.label ?? 'Unknown',
        detail: d ? (d.fix ? `${d.detail} · ${d.fix}` : d.detail) : peer.mesh_ip,
      };
    }),
  ];

  // Even radial placement, first node at the top (-90deg). R is a % of the
  // square viewport; cards are transl-centered on each point.
  const R = 34;
  const positions = nodes.map((_, i) => {
    const angle = ((-90 + (360 / nodes.length) * i) * Math.PI) / 180;
    return { x: 50 + R * Math.cos(angle), y: 50 + R * Math.sin(angle) };
  });

  const hubColor =
    status.coordinator_connection === 'connected'
      ? 'text-sage'
      : status.coordinator_connection === 'connecting'
        ? 'text-amber-500'
        : 'text-destructive';

  return (
    <div className="rounded-2xl border border-border/60 bg-card p-4 shadow-hearth-sm">
      <div className="relative mx-auto aspect-square w-full max-w-md">
        {/* Spokes. preserveAspectRatio stays uniform because the box is square,
            so each line lands exactly under its node card (positioned by %). */}
        <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full" aria-hidden="true">
          {nodes.map((node, i) => {
            const p = positions[i];
            return (
              <line
                key={node.id}
                x1="50"
                y1="50"
                x2={p?.x}
                y2={p?.y}
                stroke="currentColor"
                className={STATE_COLOR[node.state]}
                strokeWidth={0.6}
                strokeLinecap="round"
                strokeDasharray={isSolid(node.state) ? undefined : '2 2'}
                opacity={node.state === 'direct' ? 0.9 : 0.55}
              />
            );
          })}
        </svg>

        {/* Center hub — the household / coordinator. */}
        <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2">
          <div className="flex w-[96px] flex-col items-center gap-1 rounded-xl border border-primary/25 bg-background/70 px-2 py-2.5 text-center backdrop-blur">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
              <Home className="h-4 w-4 text-primary" aria-hidden="true" />
            </div>
            <span className="text-[11px] font-medium text-foreground">Household</span>
            <span className="flex items-center gap-1 text-[9px] text-muted-foreground">
              <span className={cn('h-1.5 w-1.5 rounded-full bg-current', hubColor)} aria-hidden="true" />
              Coordinator
            </span>
          </div>
        </div>

        {/* Machine nodes around the hub. */}
        {nodes.map((node, i) => {
          const p = positions[i];
          return (
            <div
              key={node.id}
              className="absolute -translate-x-1/2 -translate-y-1/2"
              style={{ left: `${p?.x ?? 50}%`, top: `${p?.y ?? 50}%` }}
              title={node.detail}
            >
              <div
                className={cn(
                  'flex w-[92px] flex-col items-center gap-1 rounded-xl border bg-card px-2 py-2 text-center shadow-hearth-sm',
                  node.isSelf ? 'border-primary/40 ring-1 ring-primary/20' : 'border-border/60',
                )}
              >
                <div
                  className={cn(
                    'flex h-7 w-7 items-center justify-center rounded-lg ring-1',
                    node.isSelf ? 'bg-primary/12 ring-primary/20' : 'bg-muted ring-border/60',
                  )}
                >
                  <Monitor
                    className={cn('h-3.5 w-3.5', node.isSelf ? 'text-primary' : 'text-muted-foreground')}
                    aria-hidden="true"
                  />
                </div>
                <span className="max-w-full truncate text-[11px] font-medium text-foreground">
                  {node.isSelf ? 'You' : node.name}
                </span>
                <span className="flex items-center gap-1 text-[9px] text-muted-foreground">
                  <span
                    className={cn('h-1.5 w-1.5 shrink-0 rounded-full bg-current', STATE_COLOR[node.state])}
                    aria-hidden="true"
                  />
                  <span className="truncate">{node.label}</span>
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Legend */}
      <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
        <LegendDot className="text-sage" label="Direct" />
        <LegendDot className="text-amber-500" label="Relayed" />
        <LegendDot className="text-destructive" label="Offline" />
        <span className="text-muted-foreground/70">Each machine and how it connects right now.</span>
      </div>
    </div>
  );
}

function LegendDot({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn('h-1.5 w-1.5 rounded-full bg-current', className)} aria-hidden="true" />
      {label}
    </span>
  );
}
