## Agent skills

### Unimplemented features (read before building, delete after shipping)

`UNIMPLEMENTED.md` is the live backlog of voice-assistant interaction features that are
**not** implemented yet, together with the constraints and technical detail needed to add
them. The rules are not optional:

1. **Read it before implementing any user-visible capability** — its section 0 lists the
   constraints that have already caused bugs (Chinese-only TTS lexicon, the
   `ToolResult.message` speech contract, tier not reaching the tool layer, swallowed tick
   exceptions), and section 1 lists the cross-cutting prerequisites that make several
   features impossible to finish without.
2. **Delete the entry once the feature ships.** A partial implementation rewrites the
   entry to what is left; it never stays in the file as a trophy.
3. **Sync the docs in the same commit** — `README.md` (what a user can say, what the
   limits are), `deployment.md` (config knobs, troubleshooting), and `spec.md`'s status
   markers.
4. **Add newly discovered gaps** to the matching section instead of leaving them in a
   chat transcript.

`spec.md` §15 mirrors the same lists; if you change one, change both.

### Issue tracker

Issues live in GitHub Issues (gh CLI). See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: root CONTEXT.md + docs/adr/. See `docs/agents/domain.md`.