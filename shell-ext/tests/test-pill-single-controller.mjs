/* Single-controller / no-overlap regression tests (master audit §5).
 * Source-inspection so they run under plain node with no GNOME session.
 * Run: node --test shell-ext/tests/test-pill-single-controller.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const extDir = path.join(here, '..', 'faceid@nim');
const read = (n) => fs.readFileSync(path.join(extDir, n), 'utf8');

test('extension enable() is single-controller (no stacked pills)', () => {
    const src = read('extension.js');
    assert.match(src, /if \(this\._pill \|\| this\._client\)/);
    assert.match(src, /this\.disable\(\);/);
    const enables = (src.match(/this\._pill = new FaceIdPill\(\)/g) || []).length;
    assert.equal(enables, 1, 'exactly one pill construction site');
});

test('extension disable() fully tears down (no leaked actors/signals)', () => {
    const src = read('extension.js');
    assert.match(src, /screenShield\.disconnect\(this\._lockId\)/);
    assert.match(src, /this\._client\?\.destroy\(\)/);
    assert.match(src, /removeChrome\?\.?\(this\._pill\)/);
    assert.match(src, /this\._pill\.destroy\(\)/);
    assert.match(src, /this\._pill = null/);
});

test('new scan scrubs prior result (no stale cards/transforms)', () => {
    const src = read('pill.js');
    const start = src.slice(src.indexOf('startScanning('));
    assert.match(start.slice(0, 800), /this\.reset\(\)/);
});

test('single tick clock with guard (no parallel timers)', () => {
    const raw = read('pill.js');
    const src = raw
        .replace(/\/\/.*$/gm, '')
        .replace(/\/\*[\s\S]*?\*\//g, '');
    assert.match(src, /if \(.*_ticker.*\)\s*return/);
    assert.equal((src.match(/timeout_add\(/g) || []).length, 1);
    assert.match(src, /_stopClock\(\)/);
});

test('lock screen stays listen-only (no daemon calls, no retry)', () => {
    for (const f of ['extension.js', 'dbusClient.js', 'pill.js']) {
        const code = read(f)
            .replace(/\/\/.*$/gm, '')
            .replace(/\/\*[\s\S]*?\*\//g, '');
        assert.doesNotMatch(code, /\.call\(/, `${f} must not call D-Bus methods`);
        assert.doesNotMatch(code, /PreviewAnimation/, `${f} must not trigger previews`);
        assert.doesNotMatch(code, /TestScan/, `${f} must not trigger scans`);
    }
});

test('terminal states retire the opening splash (no overlap)', () => {
    const src = read('pill.js');
    assert.match(src, /showMatched\([\s\S]{0,400}handoff/);
    assert.match(src, /_pendingVerify/);
});

test('selected variant ID owns the display (no silent substitution)', () => {
    const src = read('opening.js');
    assert.match(src, /glance.*iphone|iphone.*glance/);
    assert.match(src, /scan_face/);
});
