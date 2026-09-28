'use strict';
// Print the number of tunnels currently in an active state (starting|ready).
const { activeCount } = require('./state-lib');
process.stdout.write(String(activeCount()));
