/**
 * A tiny, dependency-free relaxed-JSON parser.
 *
 * Weaker local models often emit "looks like JSON" inside the ```action fence:
 * single-quoted strings, trailing commas, unquoted keys. parseAction tries the
 * strict JSON.parse first and only falls back to this when that throws, so valid
 * JSON never routes through here. The grammar is otherwise standard JSON.
 *
 * Supported beyond strict JSON:
 *  - strings delimited by either " or ' (a '-string may hold raw ", and a
 *    "-string may hold raw '); escapes \" \' \\ \/ \b \f \n \r \t \uXXXX.
 *  - trailing commas in objects and arrays.
 *  - unquoted object keys matching /[A-Za-z_$][\w$]*\/.
 *
 * Not supported, on purpose: comments, NaN/Infinity/undefined. Throws a clean
 * Error (with a 0-based offset) on anything it cannot parse, including trailing
 * garbage after the top-level value. Every loop advances the cursor or throws,
 * so malformed input can never hang.
 */

/** Unquoted-key matcher, anchored (sticky) so it only matches at the cursor. */
const UNQUOTED_KEY = /[A-Za-z_$][\w$]*/y;

/** Parse relaxed JSON, throwing a clean Error on truly-broken input. */
export function parseRelaxedJson(text: string): unknown {
  let pos = 0;
  const len = text.length;

  function fail(message: string): never {
    throw new Error(`${message} at position ${pos}`);
  }

  /** Skip standard JSON whitespace (space, tab, newline, carriage return). */
  function skipWs(): void {
    while (pos < len) {
      const c = text[pos];
      if (c === " " || c === "\t" || c === "\n" || c === "\r") pos++;
      else break;
    }
  }

  function parseValue(): unknown {
    skipWs();
    if (pos >= len) fail("unexpected end of input");
    const c = text[pos];
    if (c === "{") return parseObject();
    if (c === "[") return parseArray();
    if (c === '"' || c === "'") return parseString();
    if (c === "-" || (c >= "0" && c <= "9")) return parseNumber();
    return parseKeyword();
  }

  /** A "- or '-delimited string. The opposite quote is a literal char inside. */
  function parseString(): string {
    const quote = text[pos];
    pos++; // opening quote
    let out = "";
    while (pos < len) {
      const ch = text[pos];
      if (ch === quote) {
        pos++; // closing quote
        return out;
      }
      if (ch === "\\") {
        pos++; // the backslash
        if (pos >= len) fail("unterminated escape in string");
        const esc = text[pos];
        switch (esc) {
          case '"':
            out += '"';
            break;
          case "'":
            out += "'";
            break;
          case "\\":
            out += "\\";
            break;
          case "/":
            out += "/";
            break;
          case "b":
            out += "\b";
            break;
          case "f":
            out += "\f";
            break;
          case "n":
            out += "\n";
            break;
          case "r":
            out += "\r";
            break;
          case "t":
            out += "\t";
            break;
          case "u": {
            const hex = text.slice(pos + 1, pos + 5);
            if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail('invalid "\\u" escape');
            out += String.fromCharCode(parseInt(hex, 16));
            pos += 4; // the 4 hex digits (the trailing pos++ consumes the "u")
            break;
          }
          default:
            fail(`invalid escape "\\${esc}"`);
        }
        pos++; // consume the escape char (or last \u hex digit)
        continue;
      }
      out += ch;
      pos++;
    }
    fail("unterminated string");
  }

  function parseNumber(): number {
    const start = pos;
    if (text[pos] === "-") pos++;
    while (pos < len && text[pos] >= "0" && text[pos] <= "9") pos++;
    if (text[pos] === ".") {
      pos++;
      while (pos < len && text[pos] >= "0" && text[pos] <= "9") pos++;
    }
    if (text[pos] === "e" || text[pos] === "E") {
      pos++;
      if (text[pos] === "+" || text[pos] === "-") pos++;
      while (pos < len && text[pos] >= "0" && text[pos] <= "9") pos++;
    }
    const raw = text.slice(start, pos);
    const n = Number(raw);
    if (!Number.isFinite(n)) fail(`invalid number "${raw}"`);
    return n;
  }

  function parseKeyword(): boolean | null {
    if (text.startsWith("true", pos)) {
      pos += 4;
      return true;
    }
    if (text.startsWith("false", pos)) {
      pos += 5;
      return false;
    }
    if (text.startsWith("null", pos)) {
      pos += 4;
      return null;
    }
    fail(`unexpected token "${text[pos] ?? "EOF"}"`);
  }

  /** A quoted (either style) or unquoted /[A-Za-z_$][\w$]*\/ object key. */
  function parseKey(): string {
    skipWs();
    const ch = text[pos];
    if (ch === '"' || ch === "'") return parseString();
    UNQUOTED_KEY.lastIndex = pos;
    const m = UNQUOTED_KEY.exec(text);
    if (!m || m.index !== pos) fail("expected object key");
    pos = UNQUOTED_KEY.lastIndex;
    return m[0];
  }

  function parseObject(): Record<string, unknown> {
    pos++; // "{"
    const obj: Record<string, unknown> = {};
    skipWs();
    if (text[pos] === "}") {
      pos++;
      return obj;
    }
    for (;;) {
      const key = parseKey();
      skipWs();
      if (text[pos] !== ":") fail('expected ":" after object key');
      pos++;
      obj[key] = parseValue();
      skipWs();
      const ch = text[pos];
      if (ch === ",") {
        pos++;
        skipWs();
        if (text[pos] === "}") {
          pos++; // trailing comma
          return obj;
        }
        continue;
      }
      if (ch === "}") {
        pos++;
        return obj;
      }
      fail('expected "," or "}" in object');
    }
  }

  function parseArray(): unknown[] {
    pos++; // "["
    const arr: unknown[] = [];
    skipWs();
    if (text[pos] === "]") {
      pos++;
      return arr;
    }
    for (;;) {
      arr.push(parseValue());
      skipWs();
      const ch = text[pos];
      if (ch === ",") {
        pos++;
        skipWs();
        if (text[pos] === "]") {
          pos++; // trailing comma
          return arr;
        }
        continue;
      }
      if (ch === "]") {
        pos++;
        return arr;
      }
      fail('expected "," or "]" in array');
    }
  }

  const value = parseValue();
  skipWs();
  if (pos !== len) fail("unexpected trailing characters");
  return value;
}
