import { describe, expect, it } from 'vitest';
import { parseDshReadyLine, redactDshUrl } from './dsh-url.js';

describe('parseDshReadyLine', () => {
  it('parses the authenticated loopback URL', () => {
    const ready = parseDshReadyLine('dsh web: http://127.0.0.1:41234/?token=secret-token');

    expect(ready).toEqual({
      url: 'http://127.0.0.1:41234/?token=secret-token',
      origin: 'http://127.0.0.1:41234',
      port: 41234,
    });
  });

  it('ignores the optional LAN suffix', () => {
    const ready = parseDshReadyLine(
      'dsh web: http://127.0.0.1:41234/?token=secret (LAN: http://192.168.1.2:41234/?token=secret)',
    );

    expect(ready?.port).toBe(41234);
  });

  it.each([
    'open http://127.0.0.1:41234/?token=secret',
    'dsh web: https://127.0.0.1:41234/?token=secret',
    'dsh web: http://localhost:41234/?token=secret',
    'dsh web: http://0.0.0.0:41234/?token=secret',
    'dsh web: http://127.0.0.1/?token=secret',
  ])('rejects unsafe or malformed ready line: %s', (line) => {
    expect(parseDshReadyLine(line)).toBeNull();
  });
});

describe('redactDshUrl', () => {
  it('keeps only the loopback origin', () => {
    expect(redactDshUrl('http://127.0.0.1:41234/?token=secret')).toBe('http://127.0.0.1:41234/');
  });

  it('redacts token parameters outside a full URL', () => {
    expect(redactDshUrl('token=secret')).toBe('token=<redacted>');
  });
});
