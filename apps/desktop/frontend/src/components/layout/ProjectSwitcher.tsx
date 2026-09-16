/**
 * ProjectSwitcher — compact pill in the Sidebar that shows the active project
 * and opens a keyboard-navigable dropdown to switch projects.
 *
 * Dropdown options:
 *   - "Global (no project)" → setActive(null)
 *   - Each known project from GET /memory/projects
 *   - Divider
 *   - "+ New project..." → window.prompt → setActive(normalised name)
 *
 * Keyboard: Arrow keys navigate, Enter selects, Escape closes.
 * Focus management: returns focus to the trigger on close.
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { ChevronDown, FolderOpen } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useProject } from '@/hooks/useProject';
import { normaliseProjectName } from '@/lib/projectUtils';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ProjectSwitcherProps {
  /** When true (icon-only sidebar mode) render even more compact */
  compact?: boolean;
}

export function ProjectSwitcher({ compact = false }: ProjectSwitcherProps) {
  const { projects, activeProject, setActive } = useProject();
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Build the flat option list for keyboard nav
  // options: null (global), ...project names, 'new'
  const projectNames = projects.map((p) => p.project);
  const optionValues: Array<string | null> = [null, ...projectNames, '__new__'];

  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (
        menuRef.current &&
        !menuRef.current.contains(e.target as Node) &&
        !triggerRef.current?.contains(e.target as Node)
      ) {
        close();
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open, close]);

  // Reset focus index when opening
  useEffect(() => {
    if (open) {
      const currentIdx = optionValues.indexOf(activeProject);
      setFocusIndex(currentIdx >= 0 ? currentIdx : 0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        setFocusIndex((i) => Math.min(i + 1, optionValues.length - 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        setFocusIndex((i) => Math.max(i - 1, 0));
        break;
      case 'Enter': {
        e.preventDefault();
        const val = optionValues[focusIndex];
        void selectOption(val);
        break;
      }
      case 'Escape':
        e.preventDefault();
        close();
        break;
      case 'Tab':
        close();
        break;
    }
  };

  const selectOption = async (val: string | null | undefined) => {
    if (val === '__new__') {
      close();
      const raw = window.prompt('New project name:');
      if (!raw) return;
      const name = normaliseProjectName(raw);
      if (!name) return;
      await setActive(name);
      return;
    }
    await setActive(val ?? null);
    close();
  };

  const label = activeProject ?? 'Global';

  return (
    <div className="relative px-2 pb-1 pt-2">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Active project: ${label}. Click to switch.`}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={handleKeyDown}
        className={cn(
          'flex w-full items-center gap-1.5 rounded-md px-2 py-1',
          'text-xs font-medium transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          activeProject
            ? 'bg-primary/10 text-primary hover:bg-primary/15'
            : 'bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground',
        )}
      >
        <FolderOpen className="h-3 w-3 shrink-0" aria-hidden="true" />
        {!compact && (
          <span className="flex-1 truncate text-left">{label}</span>
        )}
        <ChevronDown
          className={cn('h-3 w-3 shrink-0 transition-transform', open && 'rotate-180')}
          aria-hidden="true"
        />
      </button>

      {open && (
        <div
          ref={menuRef}
          role="listbox"
          aria-label="Select project"
          className={cn(
            'absolute left-2 right-2 top-full z-50 mt-1 rounded-lg border border-border',
            'bg-popover py-1 shadow-lg',
          )}
          onKeyDown={handleKeyDown}
        >
          {/* Global option */}
          <DropdownOption
            focused={focusIndex === 0}
            selected={activeProject === null}
            onClick={() => void selectOption(null)}
            onMouseEnter={() => setFocusIndex(0)}
          >
            <span className="text-muted-foreground">Global (no project)</span>
          </DropdownOption>

          {/* Known projects */}
          {projectNames.map((name, i) => (
            <DropdownOption
              key={name}
              focused={focusIndex === i + 1}
              selected={activeProject === name}
              onClick={() => void selectOption(name)}
              onMouseEnter={() => setFocusIndex(i + 1)}
            >
              <span className="flex items-center gap-2">
                <span className="flex-1 truncate">{name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {projects[i]?.memory_count ?? 0}m · {projects[i]?.task_count ?? 0}t
                </span>
              </span>
            </DropdownOption>
          ))}

          {/* Divider */}
          <hr className="my-1 border-border" />

          {/* New project */}
          <DropdownOption
            focused={focusIndex === optionValues.length - 1}
            selected={false}
            onClick={() => void selectOption('__new__')}
            onMouseEnter={() => setFocusIndex(optionValues.length - 1)}
          >
            <span className="text-primary">+ New project…</span>
          </DropdownOption>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DropdownOption
// ---------------------------------------------------------------------------

interface DropdownOptionProps {
  focused: boolean;
  selected: boolean;
  onClick: () => void;
  onMouseEnter: () => void;
  children: React.ReactNode;
}

function DropdownOption({ focused, selected, onClick, onMouseEnter, children }: DropdownOptionProps) {
  const ref = useRef<HTMLDivElement>(null);

  // Scroll into view when keyboard-focused
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: 'nearest' });
  }, [focused]);

  return (
    <div
      ref={ref}
      role="option"
      aria-selected={selected}
      onClick={onClick}
      onMouseEnter={onMouseEnter}
      className={cn(
        'cursor-pointer px-3 py-1.5 text-xs text-foreground transition-colors',
        focused && 'bg-accent',
        selected && !focused && 'bg-primary/10',
      )}
    >
      {children}
    </div>
  );
}
