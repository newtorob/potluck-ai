/**
 * readiness — "is this machine ready?" netcheck, composed from signals we
 * already collect: local health (sidecar + loaded model), hardware detection,
 * and the mesh diagnostics engine (deriveDiagnostics).
 *
 * This is the product surface the multi-hour two-machine bring-up should have
 * had: one glance that says whether this box can run locally, and whether it's
 * reachable on the mesh — each gap named with a fix, not a generic "offline".
 *
 * v1 composes what's already exposed. Deeper probes flagged by the product scan
 * (an actual GPU-inference test, port/firewall scan, direct VPN-conflict probe,
 * sidecar-token validity) need new backend endpoints and are a tracked follow-on;
 * we deliberately show only checks backed by real data rather than stubs.
 *
 * Privacy: metadata only (model name, GPU name, peer reachability). No prompt or
 * inference content.
 */

import { worstSeverity, type DiagSeverity, type DiagnosticsReport } from './meshDiagnostics';
import { formatModelName } from './modelName';
import type { HardwareInfo } from '@/hooks/useHardware';
import type { MeshPeer } from '@/types/mesh';

export type ReadinessGroup = 'machine' | 'network';

export interface ReadinessCheck {
  code: string;
  group: ReadinessGroup;
  severity: DiagSeverity;
  title: string;
  detail: string;
  fix?: string;
}

export interface ReadinessReport {
  checks: ReadinessCheck[];
  overall: DiagSeverity;
  /** True when no machine-group check is warn/error — i.e. this box can run
   *  inference locally. The network group is optional and never gates this. */
  machineReady: boolean;
  headline: string;
}

export interface ReadinessInput {
  sidecarUp: boolean;
  modelLoaded: boolean;
  activeModelId: string | null;
  nCtxLoaded: number | null;
  hardware: HardwareInfo | null;
  /** Mesh node enrolled (node_id present). When false, network checks collapse
   *  to a single "not connected (optional)" line. */
  enrolled: boolean;
  /** Already-computed mesh diagnostics (deriveDiagnostics output). */
  mesh: DiagnosticsReport;
  /** Peer roster, used to label per-peer network checks by name. */
  peers: MeshPeer[];
  /**
   * VPN-conflict probe from the mesh status poll. Optional — when absent
   * (sidecar offline, older sidecar, mesh not enrolled) the check is omitted
   * rather than shown as an error.
   */
  vpnConflict?: { conflict: boolean; detail: string };
  /**
   * Whether the WireGuard listen port is bound and accepting connections.
   * Optional — omitted when mesh sidecar hasn't reported it yet.
   */
  wgPortListening?: boolean;
}

export function deriveReadiness(input: ReadinessInput): ReadinessReport {
  const checks: ReadinessCheck[] = [];

  // ---- Machine (this host) -------------------------------------------------
  checks.push(
    input.sidecarUp
      ? {
          code: 'SIDECAR',
          group: 'machine',
          severity: 'ok',
          title: 'Local engine running',
          detail: 'The on-device inference sidecar is responding.',
        }
      : {
          code: 'SIDECAR',
          group: 'machine',
          severity: 'error',
          title: 'Local engine offline',
          detail: "Potluck's inference sidecar isn't responding, so nothing can run on this machine.",
          fix: 'Restart Potluck.',
        },
  );

  if (input.modelLoaded) {
    const ctx = input.nCtxLoaded ? ` · ${input.nCtxLoaded.toLocaleString()}-token context` : '';
    checks.push({
      code: 'MODEL',
      group: 'machine',
      severity: 'ok',
      title: 'Model loaded',
      detail: `${input.activeModelId ? formatModelName(input.activeModelId) : 'A model'} is loaded and ready${ctx}.`,
    });
  } else {
    checks.push({
      code: 'MODEL',
      group: 'machine',
      severity: 'warn',
      title: 'No model loaded',
      detail: "This machine can't run or serve inference until a model is loaded.",
      fix: 'Pick one in Models.',
    });
  }

  // Hardware — detection only (not an inference-time GPU test; see follow-on).
  const hw = input.hardware;
  if (!hw) {
    checks.push({
      code: 'HARDWARE',
      group: 'machine',
      severity: 'info',
      title: 'Hardware not detected yet',
      detail: "Still reading this machine's CPU/GPU.",
    });
  } else if (hw.gpu_name) {
    // Apple Silicon shares one unified-memory pool between CPU and GPU — calling
    // it "VRAM" (a dedicated discrete-GPU concept) would be wrong.
    const memLabel = /apple/i.test(hw.gpu_name) ? 'unified memory' : 'VRAM';
    const mem = hw.gpu_vram_gb ? ` · ${hw.gpu_vram_gb} GB ${memLabel}` : '';
    checks.push({
      code: 'HARDWARE',
      group: 'machine',
      severity: 'ok',
      title: 'GPU detected',
      detail: `${hw.gpu_name}${mem}. Recommended model tier: ${hw.recommended_tier}.`,
    });
  } else {
    checks.push({
      code: 'HARDWARE',
      group: 'machine',
      severity: 'info',
      title: 'No GPU detected',
      detail: `Inference runs on CPU (${hw.cpu_name}), which is slower. Small models still work.`,
    });
  }

  // ---- Network passive probes — drawn from the mesh status poll -----------
  // These are emitted regardless of enrollment: a VPN conflict matters even
  // before pairing, and the port check is the first thing to fix when pairing
  // fails. Both are gated on the signal being present — an absent field (sidecar
  // offline, older version) renders nothing rather than a false alarm.

  if (input.vpnConflict !== undefined) {
    if (input.vpnConflict.conflict) {
      checks.push({
        code: 'VPN_CONFLICT',
        group: 'network',
        severity: 'warn',
        title: 'VPN may be blocking the mesh',
        detail: input.vpnConflict.detail || 'A VPN is active that may prevent direct mesh connections.',
        fix: 'Turn off the VPN, or allow LAN and mesh routes through it. For Mullvad, stop the daemon (sudo systemctl stop mullvad-daemon). Do not just disconnect: a plain disconnect leaves the kill-switch on and can cut the mesh entirely.',
      });
    } else {
      checks.push({
        code: 'VPN_CONFLICT',
        group: 'network',
        severity: 'ok',
        title: 'No VPN conflict detected',
        detail: 'No VPN interference found on this machine.',
      });
    }
  }

  if (input.wgPortListening !== undefined) {
    if (input.wgPortListening) {
      checks.push({
        code: 'WG_PORT',
        group: 'network',
        severity: 'ok',
        title: 'Mesh port open',
        detail: 'The WireGuard port is bound and ready to accept peer connections.',
      });
    } else {
      checks.push({
        code: 'WG_PORT',
        group: 'network',
        severity: 'warn',
        title: 'Mesh port not open',
        detail: 'The WireGuard port is not listening on this machine. Peers may not be able to connect directly.',
        fix: 'Check that your firewall allows UDP on the mesh port (default 51820). If another WireGuard instance is running, stop it first.',
      });
    }
  }

  // ---- Network (mesh) — optional ------------------------------------------
  if (!input.enrolled) {
    checks.push({
      code: 'MESH',
      group: 'network',
      severity: 'info',
      title: 'Not connected to a mesh',
      detail: 'Optional. Pair another machine in My Machines to route across your own devices.',
    });
  } else {
    const nameById = new Map(input.peers.map((p) => [p.node_id, p.node_name]));
    for (const d of input.mesh.global) {
      checks.push({
        code: d.code,
        group: 'network',
        severity: d.severity,
        title: d.title,
        detail: d.detail,
        ...(d.fix !== undefined ? { fix: d.fix } : {}),
      });
    }
    const peerChecks = Object.values(input.mesh.perPeer);
    if (peerChecks.length === 0 && input.mesh.global.length === 0) {
      checks.push({
        code: 'MESH_OK',
        group: 'network',
        severity: 'ok',
        title: 'Mesh up',
        detail: 'Connected to the coordinator. No other machines paired yet.',
      });
    }
    for (const p of peerChecks) {
      const name = nameById.get(p.peerId) ?? 'A machine';
      // Relayed is "connected but suboptimal" — the diagnostics engine rates it
      // ok (it works), but My Machines colors it amber, and a green "all good"
      // check sitting next to a Fix line reads wrong. Surface it as attention
      // here to match, without alarming.
      const severity = p.state === 'relayed' ? 'warn' : p.severity;
      checks.push({
        code: `PEER_${p.peerId}`,
        group: 'network',
        severity,
        title: `${name}: ${p.label}`,
        detail: p.detail,
        ...(p.fix !== undefined ? { fix: p.fix } : {}),
      });
    }
  }

  // ---- Roll up -------------------------------------------------------------
  let overall: DiagSeverity = 'ok';
  for (const c of checks) overall = worstSeverity(overall, c.severity);

  const machineProblems = checks.filter(
    (c) => c.group === 'machine' && (c.severity === 'warn' || c.severity === 'error'),
  );
  const machineReady = machineProblems.length === 0;

  let headline: string;
  if (!machineReady) {
    const worst = machineProblems.find((c) => c.severity === 'error') ?? machineProblems[0];
    headline = worst?.title ?? 'This machine needs attention';
  } else {
    headline = input.enrolled ? 'This machine is ready' : 'This machine is ready (running locally)';
  }

  return { checks, overall, machineReady, headline };
}
