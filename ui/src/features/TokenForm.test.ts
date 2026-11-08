import { describe, expect, it } from 'vitest';
import { cleanToken } from './TokenForm';

describe('cleanToken', () => {
  it('takes a Git personal access token, which is not a JWT', () => {
    expect(cleanToken('  9f2c0a1b7d3e4f5a6b7c8d9e0f1a2b3c4d5e6f70 ')).toBe('9f2c0a1b7d3e4f5a6b7c8d9e0f1a2b3c4d5e6f70');
    expect(cleanToken('ghp_abcDEF123')).toBe('ghp_abcDEF123');
  });

  it('takes a JWT, and drops a pasted "Bearer " prefix', () => {
    expect(cleanToken('Bearer aaa.bbb.ccc')).toBe('aaa.bbb.ccc');
    expect(cleanToken('bearer   aaa.bbb.ccc')).toBe('aaa.bbb.ccc');
  });

  it('refuses nothing, and more than one token', () => {
    expect(cleanToken('')).toBeNull();
    expect(cleanToken('   ')).toBeNull();
    expect(cleanToken('two tokens')).toBeNull();
    expect(cleanToken('line one\nline two')).toBeNull();
  });
});
