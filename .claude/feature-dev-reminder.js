#!/usr/bin/env node
// SessionStart hook: inject the feature-dev flow into context so it is present
// from turn one, rather than a skill that has to be remembered and opened.
'use strict';
const fs = require('fs');
const path = require('path');
const txt = fs.readFileSync(path.join(__dirname, 'feature-dev-reminder.txt'), 'utf8');
process.stdout.write(
  JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'SessionStart',
      additionalContext: txt,
    },
  }),
);
