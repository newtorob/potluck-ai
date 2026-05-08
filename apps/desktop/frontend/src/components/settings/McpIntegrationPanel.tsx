/**
 * McpIntegrationPanel — "External agents (MCP)" section in Settings → Advanced.
 *
 * Shows per-editor JSON snippets that let Claude Code, Cursor, Zed, and Windsurf
 * connect to Potluck's MCP server.
 *
 * Binary path strategy
 * --------------------
 * DEV  (`import.meta.env.DEV`):
 *   Uses `{REPO_ROOT}/.venv/bin/python -m potluck_local.mcp --stdio`.
 *   This works the moment `pip install -r requirements.txt` completes, without
 *   requiring the optional `pip install -e .` that registers the `potluck-mcp`
 *   entry-point script. The repo root is read from VITE_POTLUCK_REPO_ROOT,
 *   falling back to the known dev machine path `/Users/sam/Documents/potluckai`.
 *   This fallback is intentionally hardcoded for dev — only ever runs locally.
 *
 * PACKAGED (`!import.meta.env.DEV`):
 *   Uses the bundled binary at `/Applications/Potluck AI.app/Contents/Resources/python/potluck-mcp`.
 *   Non-Mac users will see a note to adjust the install location.
 *   Cross-platform path detection is deferred to a future task.
 */

import { useState, useCallback, useRef, useId } from 'react';
import { Copy, Check, ChevronDown } from 'lucide-react';
import { cn } from '@/components/ui/cn';

// ---------------------------------------------------------------------------
// Binary path resolution
// ---------------------------------------------------------------------------

// DEV: fallback repo root is hardcoded to the known local machine.
// This is intentional — `import.meta.env.DEV` is always false in production.
const REPO_ROOT: string = import.meta.env['VITE_POTLUCK_REPO_ROOT'] as string | undefined
  ?? '/Users/sam/Documents/potluckai';

const VENV_PYTHON = `${REPO_ROOT}/apps/desktop/python/.venv/bin/python`;
const BUNDLED_BINARY = '/Applications/Potluck AI.app/Contents/Resources/python/potluck-mcp';

const IS_DEV: boolean = import.meta.env.DEV as boolean;

interface BinaryConfig {
  command: string;
  args: string[];
}

function resolveBinaryConfig(): BinaryConfig {
  if (IS_DEV) {
    return {
      command: VENV_PYTHON,
      args: ['-m', 'potluck_local.mcp', '--stdio'],
    };
  }
  return {
    command: BUNDLED_BINARY,
    args: ['--stdio'],
  };
}

// ---------------------------------------------------------------------------
// Snippet generators — one per editor
// ---------------------------------------------------------------------------

type EditorId = 'claude-code' | 'cursor' | 'zed' | 'windsurf';

interface EditorTab {
  id: EditorId;
  label: string;
  configFile: string;
  buildSnippet: (cfg: BinaryConfig) => string;
}

function buildMcpServersSnippet(cfg: BinaryConfig): string {
  return JSON.stringify(
    {
      mcpServers: {
        potluck: {
          command: cfg.command,
          args: cfg.args,
        },
      },
    },
    null,
    2,
  );
}

function buildZedSnippet(cfg: BinaryConfig): string {
  return JSON.stringify(
    {
      context_servers: {
        potluck: {
          command: {
            path: cfg.command,
            args: cfg.args,
          },
        },
      },
    },
    null,
    2,
  );
}

const EDITORS: EditorTab[] = [
  {
    id: 'claude-code',
    label: 'Claude Code',
    configFile: '~/.config/claude/config.json',
    buildSnippet: buildMcpServersSnippet,
  },
  {
    id: 'cursor',
    label: 'Cursor',
    configFile: 'Settings → MCP',
    buildSnippet: buildMcpServersSnippet,
  },
  {
    id: 'zed',
    label: 'Zed',
    configFile: '~/.config/zed/settings.json',
    buildSnippet: buildZedSnippet,
  },
  {
    id: 'windsurf',
    label: 'Windsurf',
    configFile: '~/.codeium/windsurf/mcp_config.json',
    buildSnippet: buildMcpServersSnippet,
  },
];

// ---------------------------------------------------------------------------
// MCP tools table data
// ---------------------------------------------------------------------------

interface McpTool {
  name: string;
  description: string;
}

const MCP_TOOLS: McpTool[] = [
  { name: 'memory_search', description: 'Search saved memories by query + filters' },
  { name: 'memory_save', description: 'Save an explicit memory' },
  { name: 'memory_list', description: 'Browse memories by category' },
  { name: 'memory_delete', description: 'Soft-delete a memory by id' },
  { name: 'task_create', description: 'Create a long-running task with steps + constraints' },
  { name: 'task_get', description: 'Fetch a task by id' },
  { name: 'task_list', description: 'List tasks, optionally filtered by status' },
  { name: 'task_update', description: 'Patch task fields' },
  { name: 'task_complete_step', description: 'Move a pending step to completed' },
  { name: 'task_add_error', description: 'Log an error encountered while working a task' },
];

// ---------------------------------------------------------------------------
// CopyButton — isolated so copied state is per-snippet, not per-panel
// ---------------------------------------------------------------------------

interface CopyButtonProps {
  snippet: string;
  editorLabel: string;
}

function CopyButton({ snippet, editorLabel }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(snippet).then(
      () => {
        setCopied(true);
        if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(() => setCopied(false), 1000);
      },
      () => {
        // Clipboard write failed silently — no console.log per spec
      },
    );
  }, [snippet]);

  return (
    // aria-live region wraps the button so "Copied!" is announced
    <span aria-live="polite" aria-atomic="true">
      <button
        type="button"
        onClick={handleCopy}
        aria-label={copied ? 'Copied!' : `Copy ${editorLabel} snippet`}
        className={cn(
          'flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors',
          'border border-border',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1',
          copied
            ? 'border-sage/40 bg-sage/10 text-sage'
            : 'bg-muted/50 text-muted-foreground hover:bg-accent hover:text-foreground',
        )}
      >
        {copied ? (
          <>
            <Check className="h-3.5 w-3.5" aria-hidden="true" />
            Copied!
          </>
        ) : (
          <>
            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            Copy
          </>
        )}
      </button>
    </span>
  );
}

// ---------------------------------------------------------------------------
// SnippetBlock — code block + copy button + path note
// ---------------------------------------------------------------------------

interface SnippetBlockProps {
  editor: EditorTab;
  binaryConfig: BinaryConfig;
}

function SnippetBlock({ editor, binaryConfig }: SnippetBlockProps) {
  const snippet = editor.buildSnippet(binaryConfig);
  const statusText = IS_DEV ? 'running in dev mode' : 'bundled with the app';
  const displayPath = IS_DEV ? binaryConfig.command : binaryConfig.command;

  return (
    <div className="space-y-2">
      {/* Code block header */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          Add to{' '}
          <code className="font-mono text-xs text-foreground">{editor.configFile}</code>
        </span>
        <CopyButton snippet={snippet} editorLabel={editor.label} />
      </div>

      {/* Code block */}
      <div className="relative overflow-hidden rounded-lg border border-border">
        <pre className="overflow-x-auto p-4 text-xs leading-relaxed">
          <code className="language-json">{snippet}</code>
        </pre>
      </div>

      {/* Binary path note */}
      <p className="text-xs text-muted-foreground">
        Binary path:{' '}
        <code className="font-mono text-xs text-foreground">{displayPath}</code>{' '}
        <span className="text-muted-foreground/70">({statusText})</span>
        {!IS_DEV && (
          <span className="text-muted-foreground/60">
            {' '}— macOS. Adjust path for your install location.
          </span>
        )}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AvailableTools — collapsed details / summary
// ---------------------------------------------------------------------------

function AvailableTools() {
  // Split tools into two columns for the table layout
  const leftCol = MCP_TOOLS.slice(0, 5);
  const rightCol = MCP_TOOLS.slice(5);

  return (
    <details className="group">
      <summary
        className={cn(
          'flex cursor-pointer select-none list-none items-center gap-2',
          'text-xs font-medium text-muted-foreground',
          'hover:text-foreground transition-colors',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded',
          '[&::-webkit-details-marker]:hidden',
        )}
      >
        <ChevronDown
          className="h-3.5 w-3.5 transition-transform group-open:rotate-180"
          aria-hidden="true"
        />
        Available tools
      </summary>

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-0">
        {/* Column headers */}
        <div className="col-span-2 grid grid-cols-2 gap-x-4 border-b border-border pb-1.5 mb-1.5">
          <span className="font-serif text-[10px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>Tool</span>
          <span className="font-serif text-[10px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>What it does</span>
        </div>

        {/* Left column */}
        <div className="space-y-2">
          {leftCol.map((tool) => (
            <ToolRow key={tool.name} tool={tool} />
          ))}
        </div>

        {/* Right column */}
        <div className="space-y-2">
          {rightCol.map((tool) => (
            <ToolRow key={tool.name} tool={tool} />
          ))}
        </div>
      </div>
    </details>
  );
}

function ToolRow({ tool }: { tool: McpTool }) {
  return (
    <div className="flex flex-col gap-0.5">
      <code className="text-xs font-mono text-foreground">{tool.name}</code>
      <span className="text-xs text-muted-foreground">{tool.description}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// McpIntegrationPanel — main export
// ---------------------------------------------------------------------------

export function McpIntegrationPanel() {
  const [activeEditor, setActiveEditor] = useState<EditorId>('claude-code');
  const binaryConfig = resolveBinaryConfig();
  const tablistId = useId();

  const currentEditor = EDITORS.find((e) => e.id === activeEditor) ?? EDITORS[0];

  const handleTabKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>, idx: number) => {
      const count = EDITORS.length;
      let targetIdx: number | null = null;

      if (e.key === 'ArrowRight') {
        targetIdx = (idx + 1) % count;
      } else if (e.key === 'ArrowLeft') {
        targetIdx = (idx - 1 + count) % count;
      } else if (e.key === 'Home') {
        targetIdx = 0;
      } else if (e.key === 'End') {
        targetIdx = count - 1;
      }

      if (targetIdx !== null) {
        e.preventDefault();
        const target = EDITORS[targetIdx];
        if (target) {
          setActiveEditor(target.id);
          document.getElementById(`mcp-tab-${target.id}`)?.focus();
        }
      }
    },
    [],
  );

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-5">
      {/* Section heading */}
      <h2 className="font-serif text-[11px] italic text-muted-foreground" style={{ letterSpacing: '0.04em' }}>External agents (MCP)</h2>

      {/* Description */}
      <p className="text-xs text-muted-foreground leading-relaxed">
        Potluck speaks the Model Context Protocol — your coding agent can search memories, save
        explicit ones, and manage long-running task state across sessions. Add the snippet below
        to your agent&rsquo;s MCP config to plug Potluck in.
      </p>

      {/* Editor tabs */}
      <div className="space-y-3">
        <div
          role="tablist"
          aria-label="Editor MCP config snippets"
          id={tablistId}
          className="flex gap-1"
        >
          {EDITORS.map((editor, idx) => (
            <button
              key={editor.id}
              id={`mcp-tab-${editor.id}`}
              role="tab"
              type="button"
              aria-selected={activeEditor === editor.id}
              aria-controls={`mcp-tabpanel-${editor.id}`}
              tabIndex={activeEditor === editor.id ? 0 : -1}
              onClick={() => setActiveEditor(editor.id)}
              onKeyDown={(e) => handleTabKeyDown(e, idx)}
              className={cn(
                'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1',
                activeEditor === editor.id
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-foreground',
              )}
            >
              {editor.label}
            </button>
          ))}
        </div>

        {/* Tab panels */}
        {EDITORS.map((editor) => (
          <div
            key={editor.id}
            id={`mcp-tabpanel-${editor.id}`}
            role="tabpanel"
            aria-labelledby={`mcp-tab-${editor.id}`}
            hidden={activeEditor !== editor.id}
          >
            {currentEditor && activeEditor === editor.id && (
              <SnippetBlock editor={editor} binaryConfig={binaryConfig} />
            )}
          </div>
        ))}
      </div>

      {/* Available tools disclosure */}
      <AvailableTools />

      {/* Privacy footer */}
      <p className="text-xs text-muted-foreground/70 border-t border-border pt-4">
        Tools that read or write memory respect your Privacy &rarr; Memory toggle. Task tools
        always work.
      </p>
    </div>
  );
}
