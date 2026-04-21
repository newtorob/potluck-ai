import { NavLink } from 'react-router-dom';
import { MessageSquare, Package, Settings, CreditCard } from 'lucide-react';
import { cn } from '@/components/ui/cn';

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { to: '/chat', label: 'Chat', icon: MessageSquare },
  { to: '/models', label: 'Models', icon: Package },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/membership', label: 'Membership', icon: CreditCard },
];

/**
 * Primary navigation sidebar.
 *
 * Fixed width, full height. NavLink provides active state via the `isActive`
 * callback so we get accurate highlighting on the current route.
 *
 * TODO (guide §4): Wire the app logo / wordmark at the top once branding assets exist.
 */
export function Sidebar() {
  return (
    <nav
      className="flex w-16 shrink-0 flex-col items-center border-r border-border bg-card py-4 gap-1"
      aria-label="Main navigation"
    >
      {/* Logo mark placeholder */}
      <div className="mb-4 flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
        <span className="text-sm font-bold text-primary select-none">P</span>
      </div>

      {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              'group relative flex h-10 w-10 items-center justify-center rounded-lg transition-colors',
              'hover:bg-accent hover:text-accent-foreground',
              isActive
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground',
            )
          }
          aria-label={label}
          title={label}
        >
          <Icon className="h-5 w-5" />
          {/* Tooltip on hover */}
          <span
            className={cn(
              'pointer-events-none absolute left-full ml-2 rounded-md bg-popover px-2 py-1',
              'text-xs text-popover-foreground shadow-md border border-border',
              'opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap z-50',
            )}
          >
            {label}
          </span>
        </NavLink>
      ))}
    </nav>
  );
}
