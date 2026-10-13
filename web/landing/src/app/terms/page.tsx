import type { Metadata } from 'next';
import { promises as fs } from 'fs';
import path from 'path';
import { LegalDocument } from '@/components/LegalDocument';

export const metadata: Metadata = {
  alternates: { canonical: "/terms" },
  title: 'Terms of Service',
  description:
    'The contract between you and Potluck AI: your account, membership, and use of the desktop app and network services.',
};

export default async function TermsPage() {
  const markdown = await fs.readFile(
    path.join(process.cwd(), 'src/content/legal/terms-of-service.md'),
    'utf8',
  );
  return <LegalDocument eyebrow="Legal · Terms" markdown={markdown} />;
}
