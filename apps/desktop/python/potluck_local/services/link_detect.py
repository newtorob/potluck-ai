"""Network link-type detection - classify this node's primary link as wired / wifi.

Pipeline-parallel relays a small activation tensor through every hop on every token,
so the per-token critical path is dominated by the slowest, jitteriest link in the
chain. A WiFi link -- especially a cheap USB WiFi adapter -- is the worst case: not
just a higher mean RTT but occasional 60ms+ stalls that make token streaming uneven.
The orchestrator wants to prefer wired, low-jitter peers; this module gives each node
a cheap, self-reported `link_type` it can advertise in /peer/capabilities.

Strictly stdlib + subprocess. Detection is best-effort and never crashes: any failure
(missing tool, unexpected output, permission error) degrades to 'unknown', which the
planner treats as a mild penalty rather than a hard exclusion. The result is cached for
the process lifetime -- a machine's primary link rarely changes mid-session, and this
sits adjacent to the hot capabilities path, so we pay the subprocess cost at most once.

Deliberately torch-free and import-light: this is reached from the /peer/capabilities
hot path (via peer.py), which must stay off the torch-heavy engine import graph.

Approach per OS:
  Linux   -- find the interface carrying the default route (/proc/net/route, or the
             RFC1918-bearing interface as fallback), then test for a wireless sysfs
             node: /sys/class/net/<iface>/wireless or .../phy80211 => wifi, else wired.
  macOS   -- find the active interface (default route via `route -n get default`,
             fallback to the RFC1918 LAN IP's interface), then map it to a hardware
             port via `networksetup -listallhardwareports`: the "Wi-Fi"/"AirPort"
             port's Device => wifi; an Ethernet/Thunderbolt port => wired.
  Windows -- find the active adapter (the one with the default route via
             `Get-NetRoute`/`Get-NetIPConfiguration`), then read its
             PhysicalMediaType via `Get-NetAdapter`: 'Native 802.11' / 'Wireless WAN'
             => wifi (a USB WiFi dongle reports wireless here too); '802.3' => wired.
"""

from __future__ import annotations

import ipaddress
import logging
import platform
import re
import shutil
import socket
import subprocess

logger = logging.getLogger(__name__)

# Literal link classifications. 'unknown' is a legitimate, non-fatal state (detection
# failed or the link is exotic); the planner penalizes it mildly rather than excluding.
WIRED = "wired"
WIFI = "wifi"
UNKNOWN = "unknown"

# Process-lifetime cache. A node's primary link rarely changes mid-session and this is
# adjacent to the hot capabilities poll, so detect at most once. None => not yet detected.
_link_cache: str | None = None

# Short subprocess budget: detection must never stall the capabilities response. Each
# system tool here returns in well under a second on a healthy host; cap hard regardless.
_SUBPROCESS_TIMEOUT = 3.0


def detect_link_type() -> str:
    """Return 'wired' | 'wifi' | 'unknown' for this node's primary network link.

    Cached for the process lifetime. Never raises -- any failure yields 'unknown'."""
    global _link_cache
    if _link_cache is not None:
        return _link_cache
    try:
        _link_cache = _detect_uncached()
    except Exception as exc:  # detection is best-effort; never crash the caller
        logger.debug("link_detect_failed err=%s", type(exc).__name__, exc_info=True)
        _link_cache = UNKNOWN
    return _link_cache


def _detect_uncached() -> str:
    system = platform.system()
    if system == "Linux":
        return _detect_linux()
    if system == "Darwin":
        return _detect_macos()
    if system == "Windows":
        return _detect_windows()
    return UNKNOWN


# ---------------------------------------------------------------------------
# Shared: find the interface / LAN IP carrying our traffic
# ---------------------------------------------------------------------------


def _primary_lan_ip() -> str | None:
    """The local IP this host would use to reach the LAN, via a connect-less UDP
    socket trick (no packet is actually sent). Used to pick the active interface when
    the OS routing-table parse is unavailable. Returns None on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # RFC1918, unroutable target -- no traffic sent
        ip = s.getsockname()[0]
        # Guard against the loopback fallback some stacks return when offline.
        if ip and not ip.startswith("127."):
            return ip
        return None
    except OSError:
        return None
    finally:
        s.close()


def _is_rfc1918(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _linux_default_iface() -> str | None:
    """Interface carrying the default route, from /proc/net/route (no subprocess).
    The default route is the row whose Destination is 00000000. Falls back to the
    interface that owns the primary LAN IP if /proc/net/route is unreadable."""
    try:
        with open("/proc/net/route", "r") as f:
            lines = f.readlines()
        for line in lines[1:]:  # skip header
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "00000000":  # default route dest
                return fields[0]
    except OSError:
        pass
    return _linux_iface_for_ip(_primary_lan_ip())


def _linux_iface_for_ip(ip: str | None) -> str | None:
    """Find the interface whose address matches `ip` by scanning /sys/class/net and
    asking each for its IPv4 via the SIOCGIFADDR ioctl. Pure fallback for the rare case
    /proc/net/route gave us nothing; cheap and stdlib-only."""
    if not ip:
        return None
    import fcntl
    import struct

    try:
        import os

        for iface in os.listdir("/sys/class/net"):
            if iface == "lo":
                continue
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    packed = struct.pack("256s", iface.encode()[:15])
                    addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
                finally:
                    s.close()
                if addr == ip:
                    return iface
            except OSError:
                continue
    except OSError:
        return None
    return None


def _detect_linux() -> str:
    """wifi iff the active interface exposes a wireless sysfs node. The presence of
    /sys/class/net/<iface>/wireless or .../phy80211 is the canonical kernel signal that
    a netdev is an 802.11 device (it's how `iw`/`iwconfig` decide), and it covers USB
    WiFi adapters too since they register the same cfg80211 phy."""
    import os

    iface = _linux_default_iface()
    if not iface:
        return UNKNOWN
    base = f"/sys/class/net/{iface}"
    if os.path.exists(f"{base}/wireless") or os.path.exists(f"{base}/phy80211"):
        return WIFI
    if os.path.isdir(base):  # a real, non-wireless netdev => wired
        return WIRED
    return UNKNOWN


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def _macos_active_iface() -> str | None:
    """Interface (e.g. 'en0') carrying the default route via `route -n get default`,
    falling back to the interface owning the primary LAN IP via `ifconfig`."""
    route = shutil.which("route")
    if route:
        try:
            out = subprocess.run(
                [route, "-n", "get", "default"],
                capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
            ).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            if m:
                return m.group(1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _macos_iface_for_ip(_primary_lan_ip())


def _macos_iface_for_ip(ip: str | None) -> str | None:
    """Find which interface owns `ip` by scanning `ifconfig` output. Fallback only."""
    if not ip:
        return None
    ifconfig = shutil.which("ifconfig")
    if not ifconfig:
        return None
    try:
        out = subprocess.run(
            [ifconfig], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    cur = None
    for line in out.splitlines():
        if line and not line[0].isspace():  # 'en0: flags=...' starts a block
            cur = line.split(":", 1)[0]
        elif f"inet {ip} " in line or line.strip().startswith(f"inet {ip}"):
            return cur
    return None


def _detect_macos() -> str:
    """Map the active interface to a hardware port. `networksetup -listallhardwareports`
    lists each port name + its BSD Device; the "Wi-Fi" (older OSes: "AirPort") port's
    Device is the wireless one (usually en0 on laptops, but not guaranteed -- Ethernet
    is en0 on many desktops/minis), so we resolve the port by Device rather than assume."""
    iface = _macos_active_iface()
    if not iface:
        return UNKNOWN
    networksetup = shutil.which("networksetup")
    if not networksetup:
        return UNKNOWN
    try:
        out = subprocess.run(
            [networksetup, "-listallhardwareports"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN

    # Output is repeating 3-line blocks:
    #   Hardware Port: Wi-Fi
    #   Device: en0
    #   Ethernet Address: ...
    port_name = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port_name = line.split(":", 1)[1].strip()
        elif line.startswith("Device:"):
            dev = line.split(":", 1)[1].strip()
            if dev == iface and port_name:
                low = port_name.lower()
                if "wi-fi" in low or "wifi" in low or "airport" in low:
                    return WIFI
                return WIRED  # Ethernet / Thunderbolt Ethernet / USB LAN etc.
    return UNKNOWN


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _detect_windows() -> str:
    """Read the active adapter's PhysicalMediaType via PowerShell. We find the adapter
    on the default route (lowest-metric IPv4 route to 0.0.0.0), then map its media type:
    'Native 802.11' / 'Wireless WAN' => wifi (a USB WiFi dongle reports wireless here
    too); '802.3' => wired. One PowerShell invocation does the whole resolution."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return UNKNOWN

    # Pick the IfIndex of the best (lowest RouteMetric+InterfaceMetric) default route,
    # then report that adapter's PhysicalMediaType. -NoProfile avoids slow profile init.
    script = (
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
        "Sort-Object RouteMetric,ifMetric | Select-Object -First 1; "
        "if ($r) { "
        "(Get-NetAdapter -InterfaceIndex $r.ifIndex -ErrorAction SilentlyContinue).PhysicalMediaType "
        "}"
    )
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN
    if result.returncode != 0:
        return UNKNOWN
    media = (result.stdout or "").strip().lower()
    if not media:
        return UNKNOWN
    if "802.11" in media or "wireless" in media:  # 'Native 802.11', 'Wireless WAN'
        return WIFI
    if "802.3" in media:  # wired Ethernet
        return WIRED
    return UNKNOWN
