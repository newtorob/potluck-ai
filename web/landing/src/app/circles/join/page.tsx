import type { Metadata } from 'next';
import { JoinClient } from './join-client';

export const metadata: Metadata = {
  title: 'Join a trusted circle', robots: { index: false, follow: false }, referrer: 'no-referrer',
};

export default function JoinCirclePage() {
  return <JoinClient />;
}
