'use strict';
// Stop one feature's tunnel and drop its state entry.
// Usage: node stop-tunnel.js <slug>
// Kills the tracked pid's process group. serve.sh launches the ngrok v3 agent
// and `expo start` together under one setsid group led by that pid, so killing
// the group reaps both. Missing pid / already-dead process is not an error. We
// still remove the entry.

const { get, remove } = require('./state-lib');

const slug = process.argv[2];
if (!slug) {
  process.stderr.write('stop-tunnel.js: <slug> is required\n');
  process.exit(1);
}

const entry = get(slug);
if (!entry) {
  process.stdout.write(`stop-tunnel: no entry for ${slug}\n`);
  process.exit(0);
}

function kill(pid, signal) {
  // Negative pid targets the whole process group. expo is launched with its own
  // group (setsid/nohup), so this reaps the ngrok child too.
  try {
    process.kill(-pid, signal);
    return true;
  } catch (_) {
    try {
      process.kill(pid, signal);
      return true;
    } catch (_) {
      return false;
    }
  }
}

if (entry.pid) {
  const killed = kill(entry.pid, 'SIGTERM');
  process.stdout.write(killed ? `stop-tunnel: SIGTERM -> pid ${entry.pid}\n` : `stop-tunnel: pid ${entry.pid} already gone\n`);
}

remove(slug);
process.stdout.write(`stop-tunnel: dropped ${slug}\n`);
