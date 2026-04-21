/**
 * Markdown rendering utility.
 *
 * v0 scaffold: returns the raw string unchanged. This is intentional —
 * chat messages render as plain text until the real renderer is wired.
 *
 * Real implementation (next task):
 *   - Use `marked` or `markdown-it` for parsing
 *   - Use `highlight.js` or `shiki` for syntax highlighting in code blocks
 *   - Sanitise output with `DOMPurify` to prevent XSS from model-generated HTML
 *   - Return a React-safe HTML string consumed by dangerouslySetInnerHTML
 *     (only after DOMPurify sanitisation)
 *
 * @param text — raw markdown string from the model
 * @returns rendered output (plain string for now; HTML string in v1)
 */
export function renderMarkdown(text: string): string {
  // TODO: replace with real markdown + code highlighting pipeline
  return text;
}
