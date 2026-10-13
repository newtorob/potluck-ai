import type { ReactNode } from 'react';

export function PageIntro({ title, children, detail }: {
  title: ReactNode;
  children: ReactNode;
  detail?: ReactNode;
}) {
  return (
    <header className="page-intro">
      <h1>{title}</h1>
      <div className="page-intro-description">{children}</div>
      {detail && <p className="page-intro-detail">{detail}</p>}
    </header>
  );
}
