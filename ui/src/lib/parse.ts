// Lightweight config parsing for previews in the browser. The orchestrator
// remains the source of truth; this only powers the playground's local
// preview.

export type ConfigFormat = 'yaml' | 'json' | 'toml' | 'env' | 'ini' | 'hcl' | 'xml' | 'properties';

const EXTENSIONS: Record<string, ConfigFormat> = {
  yaml: 'yaml',
  yml: 'yaml',
  json: 'json',
  toml: 'toml',
  env: 'env',
  ini: 'ini',
  hcl: 'hcl',
  xml: 'xml',
  properties: 'properties',
};

/** Same rule as the orchestrator: extension decides, YAML is the fallback. */
export function detectFormat(filePath: string): ConfigFormat {
  const name = filePath.split('/').pop() ?? '';
  if (name === '.env' || name.endsWith('.env')) return 'env';
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
  return EXTENSIONS[ext] ?? 'yaml';
}

export class ParseError extends Error {
  constructor(message: string, readonly line?: number) {
    super(line ? `line ${line}: ${message}` : message);
    this.name = 'ParseError';
  }
}

export type FlatConfig = Record<string, string>;

export function flattenConfig(content: string, format: ConfigFormat): FlatConfig {
  switch (format) {
    case 'json':
      return flattenJson(content);
    case 'yaml':
      return flattenYaml(content);
    case 'env':
    case 'properties':
      return flattenKeyValue(content, false);
    case 'ini':
    case 'toml':
      return flattenKeyValue(content, true);
    default:
      throw new ParseError(`${format.toUpperCase()} preview is not supported in the browser — validate against the server instead`);
  }
}

function flattenJson(content: string): FlatConfig {
  let data: unknown;
  try {
    data = JSON.parse(content);
  } catch (err) {
    throw new ParseError((err as Error).message);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new ParseError('top-level value must be an object');
  }
  const out: FlatConfig = {};
  const walk = (value: unknown, path: string) => {
    if (value && typeof value === 'object') {
      const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value);
      if (entries.length === 0) out[path] = Array.isArray(value) ? '[]' : '{}';
      for (const [k, v] of entries) walk(v, path ? `${path}.${k}` : k);
    } else {
      out[path] = value === null ? 'null' : String(value);
    }
  };
  walk(data, '');
  return out;
}

function unquote(value: string): string {
  const v = value.trim();
  if (v.length >= 2 && ((v[0] === '"' && v.endsWith('"')) || (v[0] === "'" && v.endsWith("'")))) {
    return v.slice(1, -1);
  }
  return v;
}

function stripComment(line: string): string {
  // A '#' starts a comment unless it sits inside quotes.
  let quote: string | null = null;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quote) {
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '#' && (i === 0 || /\s/.test(line[i - 1]))) {
      return line.slice(0, i);
    }
  }
  return line;
}

/** Indentation-based YAML subset: nested maps, scalar lists, comments. */
function flattenYaml(content: string): FlatConfig {
  const out: FlatConfig = {};
  const stack: { indent: number; path: string; listIndex: number }[] = [{ indent: -1, path: '', listIndex: 0 }];
  const lines = content.split(/\r?\n/);

  lines.forEach((rawLine, i) => {
    const lineNo = i + 1;
    if (/^\s*---\s*$/.test(rawLine)) return;
    const line = stripComment(rawLine).replace(/\s+$/, '');
    if (!line.trim()) return;
    if (/^\t/.test(line)) throw new ParseError('tabs are not allowed for indentation', lineNo);

    const indent = line.length - line.trimStart().length;
    const text = line.trim();
    const isItem = text.startsWith('- ') || text === '-';
    // List items may sit at the same indent as their parent key.
    while (stack.length > 1) {
      const top = stack[stack.length - 1];
      if (indent < top.indent || (indent === top.indent && !isItem)) stack.pop();
      else break;
    }
    const parent = stack[stack.length - 1];

    if (isItem) {
      const item = unquote(text.slice(1));
      const path = `${parent.path}.${parent.listIndex++}`;
      out[path.replace(/^\./, '')] = item;
      return;
    }

    const colon = text.search(/:(\s|$)/);
    if (colon <= 0) throw new ParseError(`expected "key: value", got "${text}"`, lineNo);
    const key = unquote(text.slice(0, colon));
    const value = text.slice(colon + 1).trim();
    const path = parent.path ? `${parent.path}.${key}` : key;

    if (value === '' || value === '|' || value === '>') {
      stack.push({ indent, path, listIndex: 0 });
    } else {
      out[path] = unquote(value);
    }
  });
  return out;
}

/** KEY=VALUE files; with sections, `[a.b]` prefixes the keys below it. */
function flattenKeyValue(content: string, sections: boolean): FlatConfig {
  const out: FlatConfig = {};
  let prefix = '';
  content.split(/\r?\n/).forEach((rawLine, i) => {
    const line = stripComment(rawLine.replace(/^\s*;.*$/, '')).trim();
    if (!line) return;
    if (sections && /^\[.+\]$/.test(line)) {
      prefix = line.slice(1, -1).trim();
      return;
    }
    const m = line.match(/^(?:export\s+)?([^=:\s]+)\s*[=:]\s*(.*)$/);
    if (!m) throw new ParseError(`expected KEY=VALUE, got "${line}"`, i + 1);
    const key = prefix ? `${prefix}.${unquote(m[1])}` : unquote(m[1]);
    out[key] = unquote(m[2]);
  });
  return out;
}

const SECRET_NAME = /(pass(word)?|secret|token|api[_-]?key|private[_-]?key|dsn|credential)/i;

/** Keys that look like credentials but hold a literal instead of @secret(). */
export function plaintextSecrets(flat: FlatConfig): string[] {
  return Object.entries(flat)
    .filter(([k, v]) => SECRET_NAME.test(k.split('.').pop() ?? k) && v !== '' && !v.startsWith('@secret('))
    .map(([k]) => k);
}
