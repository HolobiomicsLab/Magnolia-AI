---
name: inbox
description: The inter-project inbox protocol — how projects message each other through projects/<name>/inbox/ (file format, reply convention, thread statuses, self-containment).
version: 1.0
last_verified: 2026-09-28
tags: [inbox, inter-project, communication, protocol]
---

# Inter-project inbox

One opencode session works on one project and cannot write into another
project's memory. The inbox is the way around that: every project has a
mailbox directory, `projects/<name>/inbox/`, and projects talk by leaving
message files in the mailbox of the project they are writing to.

## File naming and header

A message to project X from project Y lives at:

    projects/X/inbox/from-Y/YYYY-MM-DD_<slug>.md

Header block at the top of the file:

    status: open        # open | answered | closed
    from: Y             # sending project
    to: X               # receiving project
    date: YYYY-MM-DD
    reply-to: <path>    # replies only: the message this one answers

Projects may add their own fields (an idea-ticket schema, a digest pointer);
the base header above is the common part every reader can rely on.

## Message rules

- **Self-contained.** The reader cannot see the sender's memory. Quote file
  paths (not memory entry ids), name branches and commits, and carry every
  number you cite with its measurement basis and date.
- **One ask per message**, stated explicitly. End with a "what we need back"
  section when a response is required, and mark FYI-only parts clearly.
- **Nothing auto-runs.** Plain inbox messages are read-and-answer only. Only
  the agents-daemon formats (`*.task.md`) are executed, and only with the
  human's go-ahead.
- If an item is outside the receiver's area, the reply says so explicitly
  (`not-our-area`) so the sender stops waiting.

## Replies go to the sender's inbox

The reply is a new message in the **sender's** mailbox (that is where the
asking project looks), not a run dir of the answering project:

    projects/Y/inbox/from-X/YYYY-MM-DD_re-<slug>.md

Set `reply-to:` to the original message's path, and open with one line
saying what the reply answers and what the reader should do with it.

## Thread lifecycle

1. `open` — the sender deposited the message.
2. `answered` — the receiver wrote the reply, flipped the original's
   `status:` to `answered`, and added a `reply:` line with the reply's path.
3. `closed` — the sender got what it needed and marks the thread closed
   (a status-only edit on the original message or on the reply).

Either side may make status-only edits to files in its own thread. Content
edits belong to the project whose mailbox the file sits in.

## Relation to older conventions

Threads that started under a box-local convention (replies filed inside the
receiver's box, `_reply.md` suffixes) may finish under that convention; new
threads follow this rule. A project's inbox README may extend it with
project-specific schemas (for example xiulian's idea tickets from the
literature project).
