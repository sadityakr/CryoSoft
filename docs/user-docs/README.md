# docs/user-docs/

## Purpose

How-to documentation for people *operating* I2AS — upgrading an
existing installation, moving data around, recovering from a mistake in the
GUI. Not for people reading the codebase to understand how something
works; that's a folder `README.md`, a base-class docstring or
`GLOSSARY.md`.

Written for someone who has the app open and a problem to solve, not the
repository. No assumption that the reader has cloned the source.

## How to use this folder

Add a note here whenever a change is user-visible enough to need
instructions beyond what the GUI itself can carry (data-layout changes
between versions, a manual migration step, a recovery procedure). Write it
for someone with the app open, not the repository.

## Files

- `upgrading-to-session-folders.md` — experiments now live inside Session
  folders; how to bring an older experiment folder back into view.
- `renamed-state-directories.md` — the application was renamed from
  CryoSoft to I2AS; where the logs, settings and environment variables
  moved and what an existing installation has to carry across.
