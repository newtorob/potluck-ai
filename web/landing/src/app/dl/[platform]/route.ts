/**
 * /dl/[platform] — counted download redirect.
 *
 * Logs one anonymous row (platform, timestamp, coarse attribution), then 302s to the
 * real R2 file. See src/lib/downloadTracking.ts for the fail-open rules.
 * HEAD is a no-log redirect so link-preview fetchers don't inflate the count.
 */

import { NextResponse } from 'next/server';

import { isLikelyBot, logDownload, resolveDownload } from '@/lib/downloadTracking';
import { downloadAttribution } from '@/lib/attribution';
import { getAllPosts } from '@/lib/blog';

export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ platform: string }> };

export async function GET(req: Request, { params }: Ctx) {
  const { platform } = await params;
  const target = resolveDownload(platform);
  if (!target) {
    if (platform === 'mac-intel') {
      return new NextResponse('Intel Mac downloads are temporarily paused while the native build is repaired and validated.', { status: 410 });
    }
    return new NextResponse('Unknown download', { status: 404 });
  }
  if (!isLikelyBot(req.headers.get('user-agent'))) {
    const attribution = downloadAttribution(new URL(req.url), getAllPosts().map((post) => post.slug));
    await logDownload(platform, undefined, undefined, attribution);
  }
  return NextResponse.redirect(target, 302);
}

export async function HEAD(_req: Request, { params }: Ctx) {
  const { platform } = await params;
  const target = resolveDownload(platform);
  if (!target) {
    return new NextResponse(null, { status: platform === 'mac-intel' ? 410 : 404 });
  }
  return NextResponse.redirect(target, 302);
}
