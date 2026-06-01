'use strict';

/**
 * notarize.js — electron-builder afterSign hook for macOS notarization.
 *
 * Invoked automatically by electron-builder after the .app is signed.
 * Set in package.json build config: "afterSign": "scripts/notarize.js"
 *
 * Required env vars for a real notarized build:
 *   APPLE_ID                  — your Apple ID (email)
 *   APPLE_APP_SPECIFIC_PASSWORD — app-specific password from appleid.apple.com
 *   APPLE_TEAM_ID             — your 10-char Developer Team ID
 *
 * If any of these are absent, notarization is skipped and the build succeeds.
 * This allows local ad-hoc builds to work without Apple credentials.
 */

const { execFileSync } = require('child_process');
const path = require('path');

module.exports = async function notarize(context) {
  const { electronPlatformName, appOutDir, packager } = context;

  // Only run on macOS builds.
  if (electronPlatformName !== 'darwin') {
    return;
  }

  const appleId = process.env.APPLE_ID;
  const applePassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const teamId = process.env.APPLE_TEAM_ID;

  // If any credential is missing, skip notarization gracefully.
  if (!appleId || !applePassword || !teamId) {
    const missing = [
      !appleId && 'APPLE_ID',
      !applePassword && 'APPLE_APP_SPECIFIC_PASSWORD',
      !teamId && 'APPLE_TEAM_ID',
    ].filter(Boolean);
    console.log(
      `[notarize] Skipping notarization — missing env vars: ${missing.join(', ')}. ` +
      'Ad-hoc build assumed.'
    );
    return;
  }

  const appName = packager.appInfo.productFilename;
  const appPath = path.join(appOutDir, `${appName}.app`);

  console.log(`[notarize] Submitting ${appPath} for notarization...`);
  console.log(`[notarize] Apple ID: ${appleId} / Team: ${teamId}`);

  // Submit and wait for Apple's notarization service to approve.
  // --wait blocks until the submission is accepted or rejected (usually 1-5 min).
  let submitOutput;
  try {
    submitOutput = execFileSync(
      'xcrun',
      [
        'notarytool', 'submit', appPath,
        '--apple-id', appleId,
        '--password', applePassword,
        '--team-id', teamId,
        '--wait',
        '--output-format', 'json',
      ],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }
    );
  } catch (err) {
    const stderr = err.stderr || '';
    const stdout = err.stdout || '';
    console.error('[notarize] notarytool submit failed.');
    console.error('[notarize] stdout:', stdout);
    console.error('[notarize] stderr:', stderr);

    // Try to extract the submission ID for log retrieval.
    let submissionId = null;
    try {
      const parsed = JSON.parse(stdout);
      submissionId = parsed.id;
    } catch (_) { /* ignore */ }

    if (submissionId) {
      console.error(
        `[notarize] Retrieve the full notarization log with:\n` +
        `  xcrun notarytool log ${submissionId} ` +
        `--apple-id "${appleId}" --password "<app-specific-password>" ` +
        `--team-id "${teamId}"`
      );
    }

    throw new Error(`Notarization failed: ${err.message}`);
  }

  // Parse the JSON output to confirm status.
  let result;
  try {
    result = JSON.parse(submitOutput);
  } catch (_) {
    result = {};
  }

  const status = result.status || 'unknown';
  const submissionId = result.id || 'unknown';
  console.log(`[notarize] Submission ID: ${submissionId}`);
  console.log(`[notarize] Status: ${status}`);

  if (status !== 'Accepted') {
    // Print the log retrieval command before throwing.
    console.error(
      `[notarize] Notarization was not accepted (status: ${status}).\n` +
      `[notarize] Retrieve the full log with:\n` +
      `  xcrun notarytool log ${submissionId} ` +
      `--apple-id "${appleId}" --password "<app-specific-password>" ` +
      `--team-id "${teamId}"`
    );
    throw new Error(`Notarization rejected — status: ${status}`);
  }

  // Staple the notarization ticket to the .app so it travels with the bundle.
  console.log('[notarize] Stapling notarization ticket...');
  try {
    execFileSync('xcrun', ['stapler', 'staple', appPath], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    console.log('[notarize] Staple complete.');
  } catch (err) {
    // Stapling failure is non-fatal for the dmg (builder re-staples the dmg
    // separately), but log it prominently.
    console.error('[notarize] WARNING: stapler failed:', err.message);
    console.error('[notarize] The .app may require an internet check at first launch.');
  }

  console.log('[notarize] Done.');
};
