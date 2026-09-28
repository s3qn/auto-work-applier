---
name: features
description: Review FEATURES.md with Sean. Says what is done, what is approved and next, pitches new ideas (good or bad) for him to keep or drop, and updates the file with his decisions. Use when Sean says "features", "what's left", "roadmap", "give me ideas", or asks to look at the features file.
---

# Features review

`FEATURES.md` at the repo root is the single source of truth for the app's
features. Three sections: **Done**, **Next (approved, in order)**, **Ideas (not
approved)**.

## When invoked

1. Read `FEATURES.md`. Also `git log --oneline -15` to catch anything merged
   but not yet recorded; move it to Done if so.
2. Report in this shape, briefly:
   - Done since last review (if any)
   - Next, in order, one line each
   - Ideas already on the list, one line each
3. Pitch 3 to 5 **new** ideas not already in the file. For each: one sentence
   on what it is, one on why it might be worth it, and an honest note if it is
   risky, expensive, or possibly bad. Mix safe and bold ones; Sean wants to
   judge them himself.
4. Ask Sean, with AskUserQuestion, which pitched ideas to add, which existing
   ideas to promote to Next (and in what order), and which to drop.
5. Update `FEATURES.md` to match his answers. Commit it through the
   feature-dev flow (`worktree-create.sh` → edit → `worktree-accept.sh`), never
   by hand, and push if he has asked for pushes.

## Rules

- Never start building from this skill; it only maintains the list. Building
  a feature goes through plan mode and the feature-dev flow.
- Keep entries to one or two lines. No hype, no em dashes (see AGENTS.md).
- Do not silently drop or reorder anything; every change is his decision.
