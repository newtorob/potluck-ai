import type { Metadata } from 'next';
import { promises as fs } from 'fs';
import path from 'path';
import { LegalDocument } from '@/components/LegalDocument';

export const metadata: Metadata = {
  title: 'Privacy Policy',
  description:
    'What Potluck collects, what stays on your device, and what changes when you choose remote inference or optional memory sharing.',
};

export default async function PrivacyPage() {
  const markdown = await fs.readFile(
    path.join(process.cwd(), 'src/content/legal/privacy-policy.md'),
    'utf8',
  );
  return <LegalDocument eyebrow="Legal · Privacy" markdown={markdown} />;
}
