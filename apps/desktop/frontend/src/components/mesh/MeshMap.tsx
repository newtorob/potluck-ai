/**
 * MeshMap — a Deco-style topology of the household mesh, laid out to match the
 * TP-Link Deco app's network map (Rob's confirmed reference):
 *
 *   - the COORDINATOR sits at the top as the uplink (the control plane this
 *     household dials: Potluck's hosted one, or an org's on-prem one),
 *   - THIS MACHINE is the center node (the "main unit"),
 *   - OTHER MACHINES fan out below as satellites, each linked to this machine,
 *   - a plain-language health line up top ("Everything looks good").
 *
 * Spokes carry the live connection state: direct = solid, relayed/connecting =
 * dashed. It's a hand-laid radial layout, not a physics force-graph — a
 * household is a handful of nodes and a static topology reads cleaner.
 *
 * Single source of truth: it consumes the SAME deriveDiagnostics() report the
 * roster list uses, so a machine's colour here always matches its status pill
 * below. No new probing, no parallel state.
 *
 * Privacy: renders only data already on screen in the roster (names, mesh IPs,
 * connection state). No new network calls.
 */

import { AlertCircle, CheckCircle2, Cloud, Loader2, Monitor } from 'lucide-react';
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
const isSolid = (state: PeerState): boolean => state === 'direct';

/** Map the coordinator connection into the shared PeerState palette + a label. */
function coordState(status: MeshStatus): { state: PeerState; label: string } {
  switch (status.coordinator_connection) {
    case 'connected':
      return { state: 'direct', label: 'Connected' };
    case 'connecting':
      return { state: 'connecting', label: 'Reconnecting' };
    default:
      return { state: 'no_data_plane', label: 'Offline' };
  }
}

interface Pt {
  x: number;
  y: number;
}

interface PeerNode {
  peer: MeshPeer;
  state: PeerState;
  label: string;
  detail: string;
  pos: Pt;
}

// Layout anchors, in a 0..100 square viewport.
const COORD: Pt = { x: 50, y: 13 };
const SELF: Pt = { x: 50, y: 47 };
const PEER_R = 34; // satellite distance from this machine

/** Fan the peers across the lower arc below this machine. */
function peerPoint(i: number, n: number): Pt {
  const t = n === 1 ? 0.5 : i / (n - 1);
  const deg = 140 - t * 100; // 140deg .. 40deg, all below center (sin > 0)
  const a = (deg * Math.PI) / 180;
  return { x: SELF.x + PEER_R * Math.cos(a), y: SELF.y + PEER_R * Math.sin(a) };
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
  const coord = coordState(status);

  const peerNodes: PeerNode[] = peers.map((peer, i) => {
    const d = report.perPeer[peer.node_id];
    return {
      peer,
      state: d?.state ?? 'status_unknown',
      label: d?.label ?? 'Unknown',
      detail: d ? (d.fix ? `${d.detail} · ${d.fix}` : d.detail) : peer.mesh_ip,
      pos: peerPoint(i, peers.length),
    };
  });

  // Plain-language health line (Deco's "Everything looks good").
  const offline = peerNodes.filter(
    (n) => n.state === 'no_data_plane' || n.state === 'status_unknown',
  ).length;
  const relayed = peerNodes.filter((n) => n.state === 'relayed').length;
  const health = deriveHealth({
    coordConnected: status.coordinator_connection === 'connected',
    coordConnecting: status.coordinator_connection === 'connecting',
    machines: peers.length + 1,
    offline,
    relayed,
  });

  return (
    <div className="rounded-2xl border border-border/60 bg-card p-4 shadow-hearth-sm">
      {/* Health line */}
      <div className="mb-2 flex items-center justify-center gap-1.5 text-xs">
        <health.Icon
          className={cn('h-3.5 w-3.5', health.color, health.spin && 'animate-spin')}
          aria-hidden="true"
        />
        <span className="font-medium text-foreground">{health.text}</span>
      </div>

      <div className="relative mx-auto aspect-square w-full max-w-md">
        {/* Spokes. The box is square so viewBox 0..100 maps uniformly and each
            line lands exactly under its node card (positioned by %). */}
        <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full" aria-hidden="true">
          {/* uplink: coordinator -> this machine */}
          <line
            x1={COORD.x}
            y1={COORD.y}
            x2={SELF.x}
            y2={SELF.y}
            stroke="currentColor"
            className={STATE_COLOR[coord.state]}
            strokeWidth={0.6}
            strokeLinecap="round"
            strokeDasharray={isSolid(coord.state) ? undefined : '2 2'}
            opacity={coord.state === 'direct' ? 0.9 : 0.55}
          />
          {/* satellites: this machine -> each peer */}
          {peerNodes.map((n) => (
            <line
              key={n.peer.node_id}
              x1={SELF.x}
              y1={SELF.y}
              x2={n.pos.x}
              y2={n.pos.y}
              stroke="currentColor"
              className={STATE_COLOR[n.state]}
              strokeWidth={0.6}
              strokeLinecap="round"
              strokeDasharray={isSolid(n.state) ? undefined : '2 2'}
              opacity={n.state === 'direct' ? 0.9 : 0.55}
            />
          ))}
        </svg>

        {/* Coordinator (uplink) */}
        <NodeCard
          pos={COORD}
          icon={Cloud}
          title="Coordinator"
          stateColor={STATE_COLOR[coord.state]}
          stateLabel={coord.label}
          title2={`Coordinator · ${coord.label}`}
        />

        {/* This machine (center / main unit) */}
        <NodeCard
          pos={SELF}
          icon={Monitor}
          title="You"
          subtitle={status.mesh_ip ?? undefined}
          stateColor={STATE_COLOR[coord.state]}
          stateLabel="This machine"
          highlight
          title2={`This machine${status.mesh_ip ? ` (${status.mesh_ip})` : ''}`}
        />

        {/* Peer satellites */}
        {peerNodes.map((n) => (
          <NodeCard
            key={n.peer.node_id}
            pos={n.pos}
            icon={Monitor}
            title={n.peer.node_name}
            subtitle={n.peer.mesh_ip}
            stateColor={STATE_COLOR[n.state]}
            stateLabel={n.label}
            title2={n.detail}
          />
        ))}
      </div>

      {/* Legend */}
      <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
        <LegendDot className="text-sage" label="Direct" />
        <LegendDot className="text-amber-500" label="Relayed" />
        <LegendDot className="text-destructive" label="Offline" />
      </div>
    </div>
  );
}

type IconType = typeof Monitor;

function NodeCard({
  pos,
  icon: Icon,
  title,
  subtitle,
  stateColor,
  stateLabel,
  highlight = false,
  title2,
}: {
  pos: Pt;
  icon: IconType;
  title: string;
  subtitle?: string | undefined;
  stateColor: string;
  stateLabel: string;
  highlight?: boolean;
  title2?: string | undefined;
}) {
  return (
    <div
      className="absolute -translate-x-1/2 -translate-y-1/2"
      style={{ left: `${pos.x}%`, top: `${pos.y}%` }}
      title={title2}
    >
      <div
        className={cn(
          'flex w-[96px] flex-col items-center gap-0.5 rounded-xl border bg-card px-2 py-2 text-center shadow-hearth-sm',
          highlight ? 'border-primary/40 ring-1 ring-primary/20' : 'border-border/60',
        )}
      >
        <div
          className={cn(
            'flex h-7 w-7 items-center justify-center rounded-lg ring-1',
            highlight ? 'bg-primary/12 ring-primary/20' : 'bg-muted ring-border/60',
          )}
        >
          <Icon
            className={cn('h-3.5 w-3.5', highlight ? 'text-primary' : 'text-muted-foreground')}
            aria-hidden="true"
          />
        </div>
        <span className="max-w-full truncate text-[11px] font-medium text-foreground">{title}</span>
        {subtitle && (
          <span className="max-w-full truncate font-mono text-[9px] text-muted-foreground/80">
            {subtitle}
          </span>
        )}
        <span className="flex max-w-full items-center gap-1 text-[9px] text-muted-foreground">
          <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full bg-current', stateColor)} aria-hidden="true" />
          <span className="truncate">{stateLabel}</span>
        </span>
      </div>
    </div>
  );
}

function deriveHealth(args: {
  coordConnected: boolean;
  coordConnecting: boolean;
  machines: number;
  offline: number;
  relayed: number;
}): { text: string; Icon: IconType; color: string; spin?: boolean } {
  if (args.coordConnecting) {
    return { text: 'Connecting to the coordinator…', Icon: Loader2, color: 'text-muted-foreground', spin: true };
  }
  if (!args.coordConnected) {
    return { text: 'Coordinator unreachable', Icon: AlertCircle, color: 'text-destructive' };
  }
  if (args.offline > 0) {
    return {
      text: `${args.offline} machine${args.offline === 1 ? '' : 's'} offline`,
      Icon: AlertCircle,
      color: 'text-amber-500',
    };
  }
  if (args.relayed > 0) {
    return {
      text: `Mesh up · ${args.machines} machines · ${args.relayed} relayed`,
      Icon: CheckCircle2,
      color: 'text-amber-500',
    };
  }
  if (args.machines === 1) {
    return { text: 'Mesh up · just this machine', Icon: CheckCircle2, color: 'text-sage' };
  }
  return { text: `Everything looks good · ${args.machines} machines`, Icon: CheckCircle2, color: 'text-sage' };
}

function LegendDot({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn('h-1.5 w-1.5 rounded-full bg-current', className)} aria-hidden="true" />
      {label}
    </span>
  );
}
