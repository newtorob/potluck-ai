import type { Metadata } from 'next';
import { DeviceClient } from './device-client';

export const metadata: Metadata = {
  title: 'Connect your terminal', robots: { index: false, follow: false }, referrer: 'no-referrer',
};

export default function DevicePage() {
  return <DeviceClient />;
}
