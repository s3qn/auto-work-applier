# Stack

Not chosen yet. Research in progress (Python + Playwright on a dedicated Chrome
profile is the leading option). Fill this in, with the test command, once the
stack is decided; the agents in `.claude/agents/` and the feature-dev scripts
still describe the shadowing-app stack (Expo client, FastAPI backend on :8020,
phone QR tunnel) and need adapting to match.

# Writing style

Applies to everything a human reads: UI copy, code comments, JSDoc, docs and
commit messages. The `slop-check` skill holds the full rules and a scanner.

- **Never use an em dash.** Use the punctuation the sentence wants: a period for
  two independent clauses, a comma for a dependent one, a colon before a
  definition or list, parentheses around an aside. Never a bare hyphen instead.
<!-- slop-check: disable (the rule below has to quote the phrases it bans) -->
- **No hype filler in user-facing copy.** Cut the openers ("Nice!", "Perfect."),
  the empty enthusiasm ("Let's make it happen", "You're in good company") and the
  teaser asides ("One thing worth knowing…"). State what happens.
<!-- slop-check: enable -->
- **Deliberate exceptions, do not "fix" these:** en dashes in numeric ranges
  (`30–90s`, `0–10`) are correct typography, and trailing ellipses on progress
  labels (`Loading…`) are standard UI.
- **Before finishing a feature**, run the scanner and get a clean exit:
  ```bash
  node ~/.claude/skills/slop-check/scripts/scan.js
  ```
