# Potluck mesh persistence (Linux / systemd)

Keeps your household mesh alive across reboots. **Linux + systemd only.**
For the macOS and Windows service templates, see
[`packaging/daemons`](../../packaging/daemons/README.md).

## What this does

Two systemd units, one helper script:

| File | Purpose |
|---|---|
| `potluck-mesh.service` | Runs the Go mesh sidecar (`potluck-mesh`) as a system service. Survives reboots, restarts on failure. |
| `potluck-firewall.service` | Long-running self-healing watcher. Inserts the three nftables accept rules that let mesh traffic survive in the presence of Tailscale and/or Mullvad, then **reapplies them within seconds** whenever the VPN flushes them on reconnect. |
| `install-firewall-rules.sh` | The script that does the actual nft work. Detects whether Tailscale and Mullvad are present and only touches the chains it needs to. |

## Why the firewall part exists

Both Tailscale and Mullvad install their own nftables rule sets, and both
default to dropping traffic on interfaces they don't recognize:

- **Tailscale** owns the entire CGNAT range `100.64.0.0/10` and drops any
  packet from that range on a non-`tailscale0` interface. Our mesh uses the
  same range, so every Potluck packet gets dropped unless we add an explicit
  accept for `potluck0` to Tailscale's `ts-input` chain.
- **Mullvad** runs a kill-switch (`inet mullvad` table with `policy drop;`)
  that accepts traffic only on the Mullvad WG interface. Our mesh interface
  falls into the default drop on both `input` and `output` chains.

The fix is narrow: insert one accept rule per chain, scoped to `potluck0`.
The broader VPN rulesets are not modified and keep working unchanged.

Without this, anyone running Tailscale or Mullvad will see their mesh
silently die on every reboot: the box looks healthy locally, but pings
from a peer to its mesh address fail with 100% packet loss.

**Reconnects, not just reboots.** Mullvad/Proton/Nord rebuild their entire
nftables ruleset every time they (re)connect (switching servers, waking from
sleep, a network change), which silently wipes our accept rules mid-session.
The symptom is a peer that was reachable suddenly going dark ("No reachable
peers"). A one-shot install at boot can't survive this, so the firewall unit
runs `install-firewall-rules.sh --watch`: it polls the ruleset (default every
5s, `POTLUCK_FW_WATCH_INTERVAL` to tune) and reapplies our accepts within
seconds of them vanishing. Rule disappearance is the firewall-block signal.

## Install

After `setup-household.sh` has built the mesh binary and you've paired this
machine into a household:

```bash
sudo ./scripts/systemd/install-persistence.sh \
    --install \
    --coordinator-url http://<coordinator-host>:8400
```

Or via environment variable:

```bash
export POTLUCK_COORDINATOR_URL=http://<coordinator-host>:8400
sudo -E ./scripts/systemd/install-persistence.sh --install
```

Other options: `--derp-url` (or `POTLUCK_DERP_URL`) sets the relay the mesh
uses, `--mesh-bin` stages a mesh binary from another path, and
`--user-uid` / `--user-gid` set the `SUDO_UID` / `SUDO_GID` values written
into the unit (they default to the ones `sudo` provides).

The installer:
1. Creates a dedicated `potluck` system user (idempotent: `useradd --system
   --no-create-home --shell /usr/sbin/nologin`). The mesh service runs as
   this user, not root.
2. Migrates any pre-existing WG private key from
   `$SUDO_USER's $HOME/.local/state/potluck/mesh/wg.key` to
   `/var/lib/potluck/mesh/wg.key`, preserving household identity. The old
   key is **not deleted**: verify, then remove by hand.
3. Copies `install-firewall-rules.sh` to `/usr/local/lib/potluck/` and
   stages the mesh binary there too.
4. Renders both unit files (filling in mesh binary path, URLs, your UID/GID)
5. Installs to `/etc/systemd/system/`
6. `systemctl enable --now` on both
7. Prints status

You should see the firewall rules report `present` for each VPN you have
installed, and `potluck-mesh.service` should be `active (running)`.

## Verify after a reboot

```bash
sudo reboot
# wait ~30s, log back in
sudo ./scripts/systemd/install-persistence.sh --status
```

Expected: both units active, all relevant rules present, no manual
intervention needed. For example, on a machine with both VPN clients:

```
potluck-firewall.service       enabled=enabled active=active
potluck-mesh.service           enabled=enabled active=active
tailscale detected      : yes
  ts-input accept rule    : present
mullvad detected        : yes
  mullvad input accept    : present
  mullvad output accept   : present
```

Then, from another household peer, confirm traffic flows end to end:

```bash
ping -c 3 -W 2 <mesh-ip-of-this-machine>
```

## Uninstall

```bash
sudo ./scripts/systemd/install-persistence.sh --uninstall
```

This stops + disables both units, removes the unit files, runs
`install-firewall-rules.sh --uninstall` to clean up the nft rules, and
removes the helper script from `/usr/local/lib/potluck/`.

**It does not touch** your `$HOME/.potluck` state (pairing tokens, logs),
the `potluck` system user, or `/var/lib/potluck/mesh/wg.key`. The WG key
is your household identity: losing it means every peer sees you as a new
node. Re-running `--install` will pick up the same household.

To also remove the `potluck` system user (and orphan
`/var/lib/potluck/mesh`, which means losing the household key):

```bash
sudo ./scripts/systemd/install-persistence.sh --uninstall --purge-user
```

Use this only when you're fully tearing down the install and intend to
re-pair as a new node.

## Manual control

```bash
# Restart mesh (drops + reconnects all peers)
sudo systemctl restart potluck-mesh.service

# Tail logs
journalctl -u potluck-mesh.service -f

# Firewall rules self-heal after a VPN reconnect, so no manual step is needed.
# To force an immediate reapply anyway (or after editing the script):
sudo systemctl restart potluck-firewall.service

# Watch the self-heal loop react to a Mullvad reconnect in real time
journalctl -u potluck-firewall.service -f

# Just check rules
sudo /usr/local/lib/potluck/install-firewall-rules.sh --status
```

## The `potluck` system user

The mesh service runs as a dedicated, unprivileged system user named
`potluck` (UID < 1000, no home dir, `nologin` shell). It's created
automatically by `install-persistence.sh --install` and is **not** removed
by plain `--uninstall` (pass `--purge-user` for that).

`CAP_NET_ADMIN` (and `CAP_NET_RAW`) are granted to the service via systemd's
`AmbientCapabilities=`, which is what lets a non-root process create the
`potluck0` TUN device and configure WireGuard. That is the cost of not
running the sidecar as root: a much smaller blast radius if the sidecar
were ever compromised.

## State location

The WireGuard private key (your household identity on this host) lives at:

```
/var/lib/potluck/mesh/wg.key      owner=potluck:potluck mode=0600
```

systemd creates and locks down `/var/lib/potluck/mesh` via the unit's
`StateDirectory=potluck/mesh` directive. The Go sidecar reads the path
from the `POTLUCK_WG_PRIVATE_KEY_PATH` environment variable set in the
unit file.

If you previously ran `setup-household.sh` interactively as your normal
user, your key lived at `~/.local/state/potluck/mesh/wg.key`. The
installer auto-migrates it on first `--install`:

- Old key absent: clean install, sidecar generates a fresh key on first
  start (your household will see a brand-new node)
- Old key present, new key absent: installer copies it to
  `/var/lib/potluck/mesh/wg.key`, chowns to `potluck:potluck`, chmod 0600.
  The old key is **not deleted**; verify the service comes up with the
  expected public key, then delete it manually.
- Both present: installer warns and leaves both alone (the system key
  wins, because that's what systemd will read).

To move a key manually:

```bash
sudo install -d -o potluck -g potluck -m 0700 /var/lib/potluck/mesh
sudo cp /path/to/wg.key /var/lib/potluck/mesh/wg.key
sudo chown potluck:potluck /var/lib/potluck/mesh/wg.key
sudo chmod 0600 /var/lib/potluck/mesh/wg.key
sudo systemctl restart potluck-mesh.service
```

## Main sidecar service

`install-sidecar.sh` is the headless counterpart to the Electron app, which
starts the Python sidecar (port 8321) itself. It renders
`potluck-sidecar.service.template` into a systemd unit so the Python
sidecar comes up at boot on a node with no desktop UI.

```bash
sudo ./scripts/systemd/install-sidecar.sh --install                       # ./.venv, port 8321
sudo ./scripts/systemd/install-sidecar.sh --install --port 8321 --user <user>
sudo ./scripts/systemd/install-sidecar.sh --install --autoload-model <slug>
sudo ./scripts/systemd/install-sidecar.sh --uninstall
sudo ./scripts/systemd/install-sidecar.sh --status
```

`--autoload-model` loads that model on start (best effort) so the node is
ready for inference after a boot. The installer also adds a drop-in that
puts the venv's CUDA runtime libraries on `LD_LIBRARY_PATH` for GPU
llama-cpp (a no-op on a CPU-only venv), and it polls `/health` to confirm
the service came up. `--uninstall` leaves `~/.potluck` data alone.

## Caveats

- **IPC socket ownership.** The service runs as the non-root `potluck`
  user, and the local IPC socket is created with mode `0600`, so it is owned
  by that user. A process running as a different non-root user cannot open
  it; root can. The daemon only chowns the socket to another user when it
  runs as root (see `internal/localapi/listener_unix.go`), which is what the
  app-driven install described in
  [`packaging/linux`](../../packaging/linux/README.md) does.
- **Coordinator URL is baked into the unit.** If the coordinator's address
  changes, re-run `--install` with the new URL.
