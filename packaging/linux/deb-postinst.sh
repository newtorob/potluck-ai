#!/bin/bash
#
# deb-postinst.sh - Debian package postinst for Potluck AI, run as root by
# dpkg after the app files are unpacked.
#
# electron-builder generates its own default postinst (CLI symlink + SUID
# chrome-sandbox + mime/desktop db refresh). Supplying deb.afterInstall
# REPLACES that generated script, so those standard steps are replicated
# below verbatim - do not drop them or the app's launcher/sandbox break.
#
# The Potluck-specific part stages the two files the app-driven mesh install
# needs (see packaging/linux/README.md):
#   /usr/lib/potluck/mesh-install-helper.sh          (root:root 0755)
#   /usr/share/polkit-1/actions/ai.trypotluck.mesh.policy  (root:root 0644)
# pkexec refuses to run the helper unless it is root-owned and not
# group/other-writable, so the install modes here must be kept.

APP_DIR="/opt/Potluck AI"
RES_DIR="${APP_DIR}/resources"

# ---- Standard electron-builder deb steps (replicated) -----------------------
ln -sf "${APP_DIR}/potluck-ai-desktop" "/usr/bin/potluck-ai-desktop" 2>/dev/null || true

# SUID the Electron chrome sandbox helper (Electron 5+ requires this).
chmod 4755 "${APP_DIR}/chrome-sandbox" 2>/dev/null || true

if hash update-mime-database 2>/dev/null; then
  update-mime-database /usr/share/mime || true
fi
if hash update-desktop-database 2>/dev/null; then
  update-desktop-database /usr/share/applications || true
fi

# ---- Potluck mesh: stage the polkit-authorized privileged helper + action ---
if [ -f "${RES_DIR}/mesh-install-helper.sh" ] && [ -f "${RES_DIR}/ai.trypotluck.mesh.policy" ]; then
  install -d -m 0755 -o root -g root /usr/lib/potluck
  install -m 0755 -o root -g root \
    "${RES_DIR}/mesh-install-helper.sh" \
    /usr/lib/potluck/mesh-install-helper.sh

  install -d -m 0755 -o root -g root /usr/share/polkit-1/actions
  install -m 0644 -o root -g root \
    "${RES_DIR}/ai.trypotluck.mesh.policy" \
    /usr/share/polkit-1/actions/ai.trypotluck.mesh.policy

  # polkitd caches the actions directory at start; restart so the new action
  # id is visible before the user first clicks "Enable mesh". It also rescans
  # on its own at next boot, so this is best-effort.
  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart polkit >/dev/null 2>&1 \
      || systemctl restart polkit.service >/dev/null 2>&1 \
      || true
  fi
else
  echo "potluck: mesh helper resources not found under ${RES_DIR}; mesh networking will be unavailable until reinstall" >&2
fi

exit 0
