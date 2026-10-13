import type { BlogPost } from '@/lib/blog';
import { MachineConnection } from './MachineConnection';
import '@/styles/source-art.css';

export function BlogArtwork({ kind }: { kind: BlogPost['diagram'] }) {
  if (kind !== 'open-source') return <MachineConnection mode={kind} />;
  return (
    <div className="source-art">
      <div className="source-art-top"><span>Potluck</span><span>MIT license</span></div>
      <p className="source-art-title">Open source.<br /><span>On your machines.</span></p>
      <span className="source-art-repo">newtorob/potluck-ai</span>
    </div>
  );
}
