'use strict';
// Print the first free TCP port in [15000, 16000] that is not already claimed
// by another tunnel entry in the state file. Probes on 0.0.0.0 so a port that
// Metro would bind is genuinely free. Exits non-zero if the range is exhausted.

const net = require('net');
const { usedPorts } = require('./state-lib');

const LOW = 15000;
const HIGH = 16000;

function isFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.once('listening', () => srv.close(() => resolve(true)));
    srv.listen(port, '0.0.0.0');
  });
}

(async () => {
  const claimed = usedPorts();
  for (let port = LOW; port <= HIGH; port++) {
    if (claimed.has(port)) continue;
    if (await isFree(port)) {
      process.stdout.write(String(port));
      return;
    }
  }
  process.stderr.write(`no free port in ${LOW}-${HIGH}\n`);
  process.exit(1);
})();
