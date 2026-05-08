/**
 * Sidebar — primary nav + conversation list.
 *
 * Nav items are always visible. The "Conversations" section is shown only
 * when the current route is /chat. It lists all conversations sorted by
 * last-updated with active-state highlight.
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
  Settings,
  CreditCard,
  Plus,
  MoreHorizontal,
  Trash2,
  Pencil,
  X,
  Check,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useChat } from '@/hooks/useChat';
import { ProjectSwitcher } from '@/components/layout/ProjectSwitcher';
import type { AppConversation } from '@/stores/chatStore';

// ---------------------------------------------------------------------------
// Nav config
// ---------------------------------------------------------------------------

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { to: '/chat', label: 'Chat', icon: MessageSquare },
  { to: '/models', label: 'Models', icon: Package },
  { to: '/memory', label: 'Memory', icon: Brain },
  { to: '/tasks', label: 'Tasks', icon: ListTodo },
  { to: '/agents', label: 'Agents', icon: Activity },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/membership', label: 'Membership', icon: CreditCard },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Sidebar() {
  const location = useLocation();
  const isChat = location.pathname === '/chat' || location.pathname.startsWith('/chat');

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
        isChat ? 'w-56' : 'w-16',
        'transition-[width] duration-200',
      )}
      aria-label="Main navigation"
    >
      {/* Logo — Hearth wordmark.
          The serif "P" inside the ember-tinted square is the single
          piece of bespoke iconography in the app per the design brief
          (instead of a system of custom illustrations). */}
      <div className={cn(
        'flex items-center border-b border-border px-3 py-4',
        isChat ? 'justify-start gap-2.5' : 'justify-center',
      )}>
        <div className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/12 ring-1 ring-primary/20">
          <span
            className="font-serif text-xl leading-none text-primary select-none"
            style={{ transform: 'translateY(1px)' }}
            aria-hidden="true"
          >
            P
          </span>
        </div>
        {isChat && (
          <span className="font-serif text-base leading-none tracking-display text-foreground select-none">
            Potluck
          </span>
        )}
      </div>

      {/* Project switcher — above nav */}
      {isChat && <ProjectSwitcher />}

      {/* Nav links */}
      <div className={cn('flex flex-col gap-0.5 py-3', isChat ? 'px-2' : 'items-center px-3')}>
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                'group relative flex items-center rounded-lg transition-colors',
                'hover:bg-accent hover:text-accent-foreground',
                isActive ? 'bg-primary/10 text-primary' : 'text-muted-foreground',
                isChat ? 'gap-2.5 px-2.5 py-2' : 'h-10 w-10 justify-center',
              )
            }
            aria-label={label}
            title={!isChat ? label : undefined}
          >
            <Icon className="h-4 w-4 shrink-0" />
            {isChat && <span className="text-sm font-medium">{label}</span>}

            {/* Tooltip — icon-only mode */}
            {!isChat && (
              <span
                className={cn(
                  'pointer-events-none absolute left-full ml-2 rounded-md bg-popover px-2 py-1',
                  'text-xs text-popover-foreground shadow-md border border-border',
                  'opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap z-50',
                )}
              >
                {label}
              </span>
            )}
          </NavLink>
        ))}
      </div>

      {/* Conversations section — only on /chat */}
      {isChat && (
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
    </nav>
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
      {/* Section header — small-caps editorial flavor instead of the
          generic shadcn uppercase-tracked-tiny look. */}
      <div className="flex items-center justify-between px-3 py-2">
        <span
          className="font-serif text-[11px] italic text-muted-foreground"
          style={{ letterSpacing: '0.04em' }}
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

      {/* List */}
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

  // Sync rename value if title changes externally
  useEffect(() => {
    if (!renaming) setRenameValue(conversation.title);
  }, [conversation.title, renaming]);

  // Close menu on outside click
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

  // Focus rename input when entering rename mode
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

  const relativeTime = formatRelativeTime(conversation.updatedAt);

  return (
    <li>
      <div
        className={cn(
          'group relative flex items-center gap-1 rounded-lg px-2 py-1.5 cursor-pointer',
          'hover:bg-accent transition-colors',
          isActive && 'bg-primary/10',
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
              className="text-emerald-500 hover:text-emerald-400"
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
                  isActive ? 'text-primary' : 'text-foreground',
                )}
              >
                {conversation.title}
              </span>
              <span className="text-[10px] text-muted-foreground">{relativeTime}</span>
            </div>

            {/* Three-dot menu */}
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatRelativeTime(ts: number): string {
  const diff = Date.now() - ts;
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
