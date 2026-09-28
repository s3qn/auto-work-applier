'use strict';
// Upsert a per-feature test checklist into the file the dev.sean.build dashboard
// reads. This file is SEPARATE from the tunnel state file, so a tunnel status
// update (starting→ready) never clobbers the checklist. Keyed by feature slug:
//   ~/.claude/dev-feature-test-checklists.json
//   { "<slug>": { items: [ { text, checked }, ... ] } }
// Override the path with DEV_TEST_CHECKLISTS (used by tests).
//
// Usage: items come as a JSON array on stdin (strings, or {text[,checked]}):
//   echo '["Scan QR opens the feature","Username field accepts @handles"]' \
//     | node write-checklist.js --slug <slug>
//
// Re-running for the same slug REPLACES that slug's items (so `checked` resets:
// the feature changed, re-test it). Other slugs in the file are preserved.

const fs = require('fs');
const os = require('os');
const path = require('path');

function filePath() {
  return (
    process.env.DEV_TEST_CHECKLISTS ||
    path.join(os.homedir(), '.claude', 'dev-feature-test-checklists.json')
  );
}

function readAll() {
  try {
    const raw = fs.readFileSync(filePath(), 'utf8').trim();
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (err) {
    if (err.code === 'ENOENT') return {};
    // A corrupt file should not wedge the loop; start clean but warn.
    process.stderr.write(`write-checklist: ignoring unreadable file (${err.message})\n`);
    return {};
  }
}

// Atomic write (temp file + rename), matching state-lib.js so a concurrent read
// (the dashboard's 4s auto-refresh) never sees a half-written file.
function writeAll(map) {
  const p = filePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(map, null, 2) + '\n');
  fs.renameSync(tmp, p);
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const val = argv[i + 1];
    if (!key || !key.startsWith('--')) throw new Error(`unexpected argument: ${key}`);
    out[key.slice(2)] = val;
  }
  return out;
}

const a = parseArgs(process.argv.slice(2));
if (!a.slug) {
  process.stderr.write('write-checklist.js: --slug is required\n');
  process.exit(1);
}

let raw = '';
try {
  raw = fs.readFileSync(0, 'utf8').trim(); // fd 0 = stdin
} catch {
  raw = '';
}
if (!raw) {
  process.stderr.write(
    "write-checklist.js: pass items as a JSON array on stdin, e.g.\n" +
      "  echo '[\"verify X\",\"verify Y\"]' | node write-checklist.js --slug <slug>\n"
  );
  process.exit(1);
}

let parsed;
try {
  parsed = JSON.parse(raw);
} catch (err) {
  process.stderr.write(`write-checklist.js: stdin is not valid JSON (${err.message})\n`);
  process.exit(1);
}
if (!Array.isArray(parsed)) {
  process.stderr.write('write-checklist.js: stdin JSON must be an array of items\n');
  process.exit(1);
}

const items = parsed
  .map((it) => {
    if (typeof it === 'string') return { text: it, checked: false };
    if (it && typeof it === 'object' && typeof it.text === 'string') {
      return { text: it.text, checked: !!it.checked };
    }
    return null;
  })
  .filter(Boolean);

if (!items.length) {
  process.stderr.write('write-checklist.js: no valid items (need strings or {text} objects)\n');
  process.exit(1);
}

const map = readAll();
map[a.slug] = { items };
writeAll(map);
process.stdout.write(`${a.slug}: ${items.length} checklist item(s) written\n`);
