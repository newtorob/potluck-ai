/**
 * Sidebar — primary nav + conversation list + chrome controls.
 *
 * Behaviour overhaul (Hearth chrome refactor):
 *   - Width is user-controlled, not route-controlled. Default expanded
 *     (w-60). User can collapse to icon-only (w-16) via the toggle at
 *     the bottom-left or the global ⌘\ shortcut. Persists to localStorage.
 *   - Conversations panel ALWAYS shows on /chat regardless of width.
 *     When the sidebar is collapsed and the route is /chat, the panel
 *     hides and a single "show chats" button appears in its place — the
 *     user can pop the conversations into a temporary sheet without
 *     re-expanding the whole rail. (Sheet implementation is a follow-up;
 *     v1 simply hides the conversations when collapsed and the user can
 *     re-expand to access them — no jank, just a clean affordance.)
 *   - New bottom-left actions row: collapse toggle + theme toggle.
 *
 * Per-conversation actions (rename, delete) are exposed via a three-dot
 * menu button that appears on hover.
 */

import { useEffect, useRef, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import {
  MessageSquare,
  Package,
  Brain,
  ListTodo,
  Activity,
  Globe2,
  Home,
  Gauge,
  Settings,
  CreditCard,
  Users,
  Plus,
  MoreHorizontal,
  Trash2,
  Pencil,
  X,
  Check,
  PanelLeftClose,
  PanelLeftOpen,
  Sun,
  Moon,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useChat } from '@/hooks/useChat';
import { useTheme } from '@/hooks/useTheme';
import { ProjectSwitcher } from '@/components/layout/ProjectSwitcher';
import { PotluckLogo } from '@/components/brand/PotluckLogo';
import { relativeTimeFromMs } from '@/lib/timeFormat';
import type { AppConversation } from '@/stores/chatStore';

// ---------------------------------------------------------------------------
// Persistence — collapsed state survives reload
// ---------------------------------------------------------------------------

const COLLAPSE_KEY = 'potluck.sidebar_collapsed';

function readCollapsed(): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(COLLAPSE_KEY) === 'true';
}

// ---------------------------------------------------------------------------
// Nav config
// ---------------------------------------------------------------------------

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
}

// Primary: the product story in narrative order — chat → your own machines →
// the wider network → models. "My Machines" (route /household) is the flagship
// and sits at #2, not buried below the workspace tools.
const PRIMARY_NAV: NavItem[] = [
  { to: '/chat', label: 'Chat', icon: MessageSquare },
  { to: '/household', label: 'My Machines', icon: Home },
  { to: '/readiness', label: 'Readiness', icon: Gauge },
  { to: '/network', label: 'Network', icon: Globe2 },
  { to: '/circles', label: 'Circles', icon: Users },
  { to: '/models', label: 'Models', icon: Package },
];

// Workspace: the coding-agent / memory on-ramp surfaces, grouped under a label
// so they read as an optional cluster rather than co-equal product tabs.
const WORKSPACE_NAV: NavItem[] = [
  { to: '/memory', label: 'Memory', icon: Brain },
  { to: '/tasks', label: 'Tasks', icon: ListTodo },
  { to: '/agents', label: 'Agents', icon: Activity },
];

const SECONDARY_NAV: NavItem[] = [
  { to: '/membership', label: 'Membership', icon: CreditCard },
  { to: '/settings', label: 'Settings', icon: Settings },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Sidebar() {
  const location = useLocation();
  const isChat = location.pathname === '/chat' || location.pathname.startsWith('/chat');
  const [collapsed, setCollapsedState] = useState<boolean>(readCollapsed);

  function setCollapsed(v: boolean) {
    setCollapsedState(v);
    window.localStorage.setItem(COLLAPSE_KEY, String(v));
  }

  // ⌘\ / Ctrl+\ — global collapse toggle
  useEffect(() => {
    function handler(e: KeyboardEvent) {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key === '\\') {
        e.preventDefault();
        setCollapsed(!collapsed);
      }
    }
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collapsed]);

  const expanded = !collapsed;

  const {
    conversations,
    activeConversationId,
    createConversation,
    deleteConversation,
    switchConversation,
    renameConversation,
    isStreaming,
  } = useChat();

  return (
    <nav
      className={cn(
        'flex shrink-0 flex-col border-r border-border bg-card',
        expanded ? 'w-60' : 'w-16',
        'transition-[width] duration-200',
      )}
      aria-label="Main navigation"
    >
      {/* Logo block — Potluck mark + wordmark */}
      <div className={cn(
        'flex items-center border-b border-border px-3 py-4',
        expanded ? 'justify-start gap-2.5' : 'justify-center',
      )}>
        <div className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
          <PotluckLogo size={20} className="text-primary" aria-label="Potluck" />
        </div>
        {expanded && (
          <span className="font-serif text-base leading-none tracking-display text-foreground select-none">
            Potluck
          </span>
        )}
      </div>

      {/* Project switcher */}
      {expanded && <ProjectSwitcher />}

      {/* Primary nav — the product story */}
      <div className={cn('flex flex-col gap-0.5 pt-3 pb-2', expanded ? 'px-2' : 'items-center px-3')}>
        {PRIMARY_NAV.map((item) => (
          <NavItemLink key={item.to} item={item} expanded={expanded} />
        ))}
      </div>

      {/* Workspace group — coding-agent / memory surfaces */}
      <div className={cn('flex flex-col gap-0.5 border-t border-border/50 pt-2 pb-3', expanded ? 'px-2' : 'items-center px-3')}>
        {expanded && (
          <span className="px-2.5 pb-1 font-serif text-[11px] italic text-muted-foreground/70 tracking-hearth-label">
            Workspace
          </span>
        )}
        {WORKSPACE_NAV.map((item) => (
          <NavItemLink key={item.to} item={item} expanded={expanded} />
        ))}
      </div>

      {/* Conversations — only on /chat AND only when expanded */}
      {isChat && expanded && (
        <ConversationList
          conversations={conversations}
          activeConversationId={activeConversationId}
          isStreaming={isStreaming}
          onCreate={() => createConversation()}
          onSwitch={switchConversation}
          onDelete={deleteConversation}
          onRename={renameConversation}
        />
      )}

      {/* Spacer when no conversations panel */}
      {(!isChat || !expanded) && <div className="flex-1" />}

      {/* Secondary nav (Membership, Settings) */}
      <div className={cn(
        'border-t border-border py-2',
        expanded ? 'px-2' : 'flex flex-col items-center px-3',
      )}>
        {SECONDARY_NAV.map((item) => (
          <NavItemLink key={item.to} item={item} expanded={expanded} />
        ))}
      </div>

      {/* Chrome controls — collapse + theme */}
      <ChromeControls
        expanded={expanded}
        onToggleCollapse={() => setCollapsed(!collapsed)}
      />
    </nav>
  );
}

// ---------------------------------------------------------------------------
// NavItemLink — single primary or secondary nav item
// ---------------------------------------------------------------------------

interface NavItemLinkProps {
  item: NavItem;
  expanded: boolean;
}

function NavItemLink({ item, expanded }: NavItemLinkProps) {
  const { to, label, icon: Icon } = item;
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn(
          'group relative flex items-center rounded-lg transition-colors duration-fast',
          'hover:bg-accent hover:text-foreground',
          // Active: subtle ember surface + ember-leaning text. The 2px
          // left rail (rendered via ::before below) gives the location
          // signal without re-saturating the whole pill.
          isActive
            ? 'bg-primary/8 text-foreground before:absolute before:left-0 before:top-1/2 before:h-5 before:w-[2px] before:-translate-y-1/2 before:rounded-full before:bg-primary'
            : 'text-muted-foreground',
          expanded ? 'gap-2.5 px-2.5 py-2' : 'h-10 w-10 justify-center',
        )
      }
      aria-label={label}
      title={!expanded ? label : undefined}
    >
      {({ isActive }) => (
        <>
          <Icon
            className={cn(
              'h-4 w-4 shrink-0 transition-colors duration-fast',
              isActive && 'text-primary',
            )}
          />
          {expanded && <span className="text-sm font-medium">{label}</span>}

          {!expanded && (
            <span
              className={cn(
                'pointer-events-none absolute left-full ml-2 rounded-md bg-popover px-2 py-1',
                'text-xs text-popover-foreground shadow-hearth-sm border border-border',
                'opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap z-50',
              )}
            >
              {label}
            </span>
          )}
        </>
      )}
    </NavLink>
  );
}

// ---------------------------------------------------------------------------
// ChromeControls — collapse toggle + theme toggle, bottom-left of sidebar
// ---------------------------------------------------------------------------

interface ChromeControlsProps {
  expanded: boolean;
  onToggleCollapse: () => void;
}

function ChromeControls({ expanded, onToggleCollapse }: ChromeControlsProps) {
  const { theme, toggle: toggleTheme } = useTheme();
  const isDark = theme === 'dark';

  return (
    <div
      className={cn(
        'flex shrink-0 items-center border-t border-border py-2',
        expanded ? 'justify-between px-3' : 'flex-col gap-1 px-3',
      )}
    >
      <ChromeButton
        onClick={onToggleCollapse}
        label={expanded ? 'Collapse sidebar' : 'Expand sidebar'}
        title={`${expanded ? 'Collapse' : 'Expand'} sidebar (⌘\\)`}
        icon={expanded ? PanelLeftClose : PanelLeftOpen}
      />
      <ChromeButton
        onClick={toggleTheme}
        label={isDark ? 'Switch to Common Room (light)' : 'Switch to Hearth (dark)'}
        title={isDark ? 'Switch to light' : 'Switch to dark'}
        icon={isDark ? Sun : Moon}
      />
    </div>
  );
}

interface ChromeButtonProps {
  onClick: () => void;
  label: string;
  title: string;
  icon: LucideIcon;
}

function ChromeButton({ onClick, label, title, icon: Icon }: ChromeButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={title}
      className={cn(
        'flex h-7 w-7 items-center justify-center rounded-md',
        'text-muted-foreground hover:text-foreground hover:bg-accent transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      )}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
}

// ---------------------------------------------------------------------------
// ConversationList
// ---------------------------------------------------------------------------

interface ConversationListProps {
  conversations: AppConversation[];
  activeConversationId: string | null;
  isStreaming: boolean;
  onCreate: () => void;
  onSwitch: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

function ConversationList({
  conversations,
  activeConversationId,
  isStreaming,
  onCreate,
  onSwitch,
  onDelete,
  onRename,
}: ConversationListProps) {
  return (
    <div className="flex flex-1 flex-col overflow-hidden border-t border-border">
      <div className="flex items-center justify-between px-3 py-2">
        <span
          className="font-serif text-[11px] italic text-muted-foreground tracking-hearth-label"
        >
          Chats
        </span>
        <button
          type="button"
          onClick={onCreate}
          disabled={isStreaming}
          aria-label="New chat"
          title="New chat"
          className={cn(
            'flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground',
            'hover:bg-accent hover:text-accent-foreground transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            isStreaming && 'cursor-not-allowed opacity-50',
          )}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>

      <ul
        className="flex-1 overflow-y-auto px-2 pb-3"
        aria-label="Conversations"
      >
        {conversations.length === 0 ? (
          <li className="px-2 py-2 text-xs text-muted-foreground">No conversations yet</li>
        ) : (
          conversations.map((convo) => (
            <ConversationItem
              key={convo.id}
              conversation={convo}
              isActive={convo.id === activeConversationId}
              onSwitch={onSwitch}
              onDelete={onDelete}
              onRename={onRename}
            />
          ))
        )}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ConversationItem
// ---------------------------------------------------------------------------

interface ConversationItemProps {
  conversation: AppConversation;
  isActive: boolean;
  onSwitch: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

function ConversationItem({
  conversation,
  isActive,
  onSwitch,
  onDelete,
  onRename,
}: ConversationItemProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(conversation.title);
  const menuRef = useRef<HTMLDivElement>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!renaming) setRenameValue(conversation.title);
  }, [conversation.title, renaming]);

  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [menuOpen]);

  useEffect(() => {
    if (renaming) {
      setTimeout(() => renameInputRef.current?.select(), 0);
    }
  }, [renaming]);

  function commitRename() {
    const trimmed = renameValue.trim();
    if (trimmed && trimmed !== conversation.title) {
      onRename(conversation.id, trimmed);
    }
    setRenaming(false);
  }

  function cancelRename() {
    setRenameValue(conversation.title);
    setRenaming(false);
  }

  const relativeTime = relativeTimeFromMs(conversation.updatedAt);

  return (
    <li>
      <div
        className={cn(
          'group relative flex items-center gap-1 rounded-lg px-2 py-1.5 cursor-pointer',
          'hover:bg-accent transition-colors duration-fast',
          // Same ember-rail treatment as primary nav items, scaled smaller
          isActive &&
            'bg-primary/8 before:absolute before:left-0 before:top-1/2 before:h-4 before:w-[2px] before:-translate-y-1/2 before:rounded-full before:bg-primary',
        )}
        onClick={() => !renaming && onSwitch(conversation.id)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if ((e.key === 'Enter' || e.key === ' ') && !renaming) {
            e.preventDefault();
            onSwitch(conversation.id);
          }
        }}
        aria-current={isActive ? 'page' : undefined}
        aria-label={`Conversation: ${conversation.title}`}
      >
        {renaming ? (
          <div className="flex flex-1 items-center gap-1" onClick={(e) => e.stopPropagation()}>
            <input
              ref={renameInputRef}
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename();
                if (e.key === 'Escape') cancelRename();
              }}
              onBlur={commitRename}
              className={cn(
                'flex-1 rounded border border-input bg-background px-1 py-0.5',
                'text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring',
              )}
              aria-label="Rename conversation"
            />
            <button
              type="button"
              onClick={commitRename}
              aria-label="Confirm rename"
              className="text-sage hover:text-sage/80"
            >
              <Check className="h-3 w-3" />
            </button>
            <button
              type="button"
              onClick={cancelRename}
              aria-label="Cancel rename"
              className="text-muted-foreground hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        ) : (
          <>
            <div className="flex min-w-0 flex-1 flex-col">
              <span
                className={cn(
                  'truncate text-xs font-medium',
                  isActive ? 'text-foreground' : 'text-foreground/85',
                )}
              >
                {conversation.title}
              </span>
              <span className="text-[10px] text-muted-foreground/80">{relativeTime}</span>
            </div>

            <div
              ref={menuRef}
              className="relative"
              onClick={(e) => e.stopPropagation()}
            >
              <button
                type="button"
                onClick={() => setMenuOpen((v) => !v)}
                aria-label="Conversation options"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                className={cn(
                  'flex h-5 w-5 items-center justify-center rounded text-muted-foreground',
                  'opacity-0 group-hover:opacity-100 transition-opacity',
                  'hover:bg-muted hover:text-foreground',
                  menuOpen && 'opacity-100',
                )}
              >
                <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
              </button>

              {menuOpen && (
                <div
                  role="menu"
                  className={cn(
                    'absolute right-0 top-full z-50 mt-1 w-32 rounded-lg border border-border',
                    'bg-popover py-1 shadow-lg',
                  )}
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); setRenaming(true); }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-xs hover:bg-accent transition-colors"
                  >
                    <Pencil className="h-3 w-3" aria-hidden="true" />
                    Rename
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onDelete(conversation.id); }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-destructive hover:bg-destructive/10 transition-colors"
                  >
                    <Trash2 className="h-3 w-3" aria-hidden="true" />
                    Delete
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </li>
  );
}

