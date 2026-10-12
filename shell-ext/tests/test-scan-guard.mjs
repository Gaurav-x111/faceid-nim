/* Unit tests for ../faceid@nim/scanGuard.js (master audit 5F).
 * Plain node, no GNOME session: the module is dependency-free.
 * Run: node --test shell-ext/tests/test-scan-guard.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { filterTerminal, TERMINAL, TERMINAL_GUARD_MS } from '../faceid@nim/scanGuard.js';

test('first terminal always shows', () => {
    const r = filterTerminal('matched', 10000, null);
    assert.equal(r.show, true);
    assert.deepEqual(r.last, { state: 'matched', at: 10000 });
});

test('stale opposite terminal inside the window is dropped', () => {
    const last = { state: 'matched', at: 10000 };
    for (const s of ['rejected', 'timeout', 'camera_error']) {
        const r = filterTerminal(s, 10000 + TERMINAL_GUARD_MS - 1, last);
        assert.equal(r.show, false, `${s} must be dropped as stale`);
        assert.deepEqual(r.last, last, 'stale drop keeps the fresher terminal');
    }
});

test('opposite terminal after the window shows (legit new attempt)', () => {
    const last = { state: 'matched', at: 10000 };
    const r = filterTerminal('rejected', 10000 + TERMINAL_GUARD_MS + 1, last);
    assert.equal(r.show, true);
    assert.deepEqual(r.last, { state: 'rejected', at: 10000 + TERMINAL_GUARD_MS + 1 });
});

test('same terminal repeats are never suppressed', () => {
    const last = { state: 'rejected', at: 5000 };
    const r = filterTerminal('rejected', 5500, last);
    assert.equal(r.show, true);
});

test('waking/searching clear the guard for a fresh attempt', () => {
    const last = { state: 'matched', at: 10000 };
    let r = filterTerminal('waking', 11000, last);
    assert.equal(r.show, true);
    assert.equal(r.last, null);
    r = filterTerminal('rejected', 11500, r.last);
    assert.equal(r.show, true, 'fresh attempt must not be filtered');
});

test('non-terminal, non-opening states pass through untouched', () => {
    const last = { state: 'matched', at: 10000 };
    const r = filterTerminal('verifying', 10500, last);
    assert.equal(r.show, true);
    assert.deepEqual(r.last, last);
});

test('duplicated matched delivery shows twice (idempotent display)', () => {
    const first = filterTerminal('matched', 20000, null);
    const second = filterTerminal('matched', 20100, first.last);
    assert.equal(first.show && second.show, true);
});
