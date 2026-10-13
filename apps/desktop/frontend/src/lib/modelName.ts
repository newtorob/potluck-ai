/**
 * Human-readable display names for model slugs.
 *
 * Raw slugs come in two flavors - Ollama ("qwen2.5:3b-instruct-q4_K_M") and
 * llama-cpp ("qwen-2.5-3b-instruct-q4") - plus the dev stub ("stub-echo").
 * These are fine in logs but must never be shown bare in the UI: they read as
 * leaked internals (especially "stub-echo"). Render `formatModelName(slug)` and
 * keep the raw slug in a `title` tooltip for power users.
 */

const KNOWN: Record<string, string> = {
  'stub-echo': 'Echo (diagnostic)',
  'qwen2.5:3b-instruct-q4_k_m': 'Qwen2.5 3B Instruct',
  'qwen-2.5-3b-instruct-q4': 'Qwen2.5 3B Instruct',
  'qwen2.5:7b-instruct-q4_k_m': 'Qwen2.5 7B Instruct',
  'llama3.2:3b-instruct-q4_k_m': 'Llama 3.2 3B Instruct',
  'llama-3.2-3b-instruct-q4': 'Llama 3.2 3B Instruct',
};

// Quantization / format tokens to drop from the display name (q4_K_M, q5_0,
// fp16, bf16, int8, gguf, …), including a leading separator.
const QUANT = /[-_:.]?(q\d+(_[a-z0-9]+)*|fp?16|bf16|int8|gguf)\b/gi;
const ACRONYMS = new Set(['ai', 'moe', 'llm']);

export function formatModelName(slug: string | null | undefined): string {
  if (!slug) return 'Unknown model';
  const key = slug.trim().toLowerCase();
  if (KNOWN[key]) return KNOWN[key];

  // Generic fallback: strip quant/format tokens, split on separators,
  // title-case words, uppercase size tokens (3b -> 3B) and known acronyms.
  const cleaned = slug.replace(QUANT, ' ').replace(/[:_/]+/g, ' ').replace(/-+/g, ' ');
  const words = cleaned.split(/\s+/).filter(Boolean);
  if (words.length === 0) return slug;
  return words
    .map((w) => {
      const lw = w.toLowerCase();
      if (ACRONYMS.has(lw)) return lw.toUpperCase();
      if (/^\d+(\.\d+)?b$/i.test(w)) return w.toUpperCase(); // 3b / 70b / 1.5b -> 3B
      if (/^\d/.test(w)) return w; // keep version-ish tokens (2.5)
      return w.charAt(0).toUpperCase() + w.slice(1);
    })
    .join(' ');
}
