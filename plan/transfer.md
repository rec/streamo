# Context not recorded elsewhere

- The user is testing broadcasts through showCo on a Raspberry Pi previously
  reached as `tom@t.local`. That address worked during this chat; availability
  has not been checked again for this handover.
- An unresolved operator report: the first broadcast had sound, but a later
  broadcast did not. Troubleshooting instructions were provided, but no status
  readings were collected during the silent broadcast and no cause was established.
  Do not assume that subsequent audio-related commits resolved that observation.
- The user initially said the broadcast looked great, then explicitly corrected
  that statement: they had not seen the revised photo presentation yet. There is
  no confirmed live visual acceptance of the latest photo timing or closing card.
- The photo and closing-card changes made in this chat were committed and pushed,
  but this context did not deploy them or restart the Pi's services. showCo's
  dependency update does not establish which revision the Pi is currently running.
- Other agents have been working in sibling repositories. Recheck their working
  trees before making cross-project changes. Preserve the untracked `open-loop.md`
  in this checkout; it is unrelated to the current work.

Implementation details, defaults, completed fixes, and remaining feature proposals
are deliberately omitted: use the existing documentation, plans, and commits.
