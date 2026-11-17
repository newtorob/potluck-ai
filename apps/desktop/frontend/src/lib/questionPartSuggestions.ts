/** Literal review aids; shared conditions stay in one question rather than guessed parts. */
export function suggestQuestionParts(question: string): { parts: string[]; note: string } {
  const text = question;
  const whole = {
    parts: text.trim() ? [text] : [],
    note: 'Kept together so the original wording and shared conditions stay attached.',
  };
  if (!text.trim() || text.length > 500) return whole;
  // Quotes, lists and conditions can change the scope of a later clause.
  if (
    /["'`“”‘’\n\r]/.test(text) ||
    /\b(if|unless|except|provided|before|after|until|without|not|never)\b/i.test(text)
  )
    return whole;
  const boundary =
    /(?<=\?)\s+(?=(?:what|when|where|which|who|why|how|can|could|does|do|did|is|are|will|should)\b)|\s+(?:and|but)\s+(?=(?:what|when|where|which|who|why|how)\b)/gi;
  const starts = [0, ...Array.from(text.matchAll(boundary), (match) => match.index)];
  const parts = starts.map((start, index) => text.slice(start, starts[index + 1]));
  if (
    parts.length === 1 ||
    parts.length > 6 ||
    new Set(parts.map((part) => part.trim().toLowerCase())).size !== parts.length
  )
    return whole;
  return {
    parts,
    note: 'Suggested from your original words. Check every requested part and any shared conditions.',
  };
}
