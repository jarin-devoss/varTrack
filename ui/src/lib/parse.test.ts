import { describe, expect, it } from 'vitest';
import { detectFormat, flattenConfig, ParseError, plaintextSecrets } from './parse';

describe('detectFormat', () => {
  it('uses the extension and falls back to yaml', () => {
    expect(detectFormat('configs/app.yaml')).toBe('yaml');
    expect(detectFormat('configs/app.yml')).toBe('yaml');
    expect(detectFormat('flags.JSON')).toBe('json');
    expect(detectFormat('payments.toml')).toBe('toml');
    expect(detectFormat('config/api.env')).toBe('env');
    expect(detectFormat('.env')).toBe('env');
    expect(detectFormat('billing.ini')).toBe('ini');
    expect(detectFormat('Makefile')).toBe('yaml');
  });
});

describe('flattenConfig', () => {
  it('flattens nested yaml maps and lists with dotted keys', () => {
    const flat = flattenConfig(
      [
        '# comment',
        'app:',
        '  name: checkout   # trailing comment',
        '  region: "us-east-1"',
        'locales:',
        '- en',
        '- fr',
        'database:',
        '  pool:',
        '    size: 10',
        '  host: "a#b"',
      ].join('\n'),
      'yaml',
    );
    expect(flat).toEqual({
      'app.name': 'checkout',
      'app.region': 'us-east-1',
      'locales.0': 'en',
      'locales.1': 'fr',
      'database.pool.size': '10',
      'database.host': 'a#b',
    });
  });

  it('flattens json, keeping empty containers', () => {
    expect(flattenConfig('{"a":{"b":1,"c":[true,null]},"d":{}}', 'json')).toEqual({
      'a.b': '1',
      'a.c.0': 'true',
      'a.c.1': 'null',
      d: '{}',
    });
  });

  it('reads env files and ini/toml sections', () => {
    expect(flattenConfig('export API_PORT=8080\n# c\nLOG_LEVEL="info"', 'env')).toEqual({ API_PORT: '8080', LOG_LEVEL: 'info' });
    expect(flattenConfig('[smtp]\nhost = mail\n; note\nport=587', 'ini')).toEqual({ 'smtp.host': 'mail', 'smtp.port': '587' });
  });

  it('reports the failing line', () => {
    expect(() => flattenConfig('a: 1\nnot valid', 'yaml')).toThrow(/line 2/);
    expect(() => flattenConfig('[1,2]', 'json')).toThrow(ParseError);
    expect(() => flattenConfig('<a/>', 'xml')).toThrow(/not supported/);
  });
});

describe('plaintextSecrets', () => {
  it('flags credential-like keys that are not @secret references', () => {
    expect(
      plaintextSecrets({
        'database.password': 'hunter2',
        'api.secret_key': '@secret(vault:a#b)',
        'smtp.port': '587',
        'stripe.api_key': 'sk_live_x',
      }),
    ).toEqual(['database.password', 'stripe.api_key']);
  });
});
