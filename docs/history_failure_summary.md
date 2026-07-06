# History Failure Summary Scope

`ci-owner-agent` uses deterministic history similarity only for structured test failure summaries that are stable enough to inherit responsibility across builds.

Currently supported deterministic summary formats:

- Mocha failure blocks such as `1) some test title`
- Japa / xfail-style blocks such as `✖ some test title`

Docker, BuildKit, Jenkins, and shell wrapper errors are not treated as stable historical signatures. Examples include outer errors such as:

- `ERROR: process "/bin/sh -c ..."`
- `ERROR: failed to solve:`
- generic shell exit wrappers

TypeScript compilation failures, npm install failures, Docker build failures, lint failures, and other non-structured build failures are still analyzed for the current build by the agent through log and diff tools. They are not currently saved as deterministic `ci_failure_chunks` for inherited-owner matching.

Future work may add AI failure facts for semantic history comparison of non-structured failures.

## Cleaning Old Bad Chunks

Older runs may have stored over-generalized fatal chunks, for example:

```text
signatureKey = fatal|fatal error|fatal error||
anchorType = fatal_error_block
```

After this change, use one of these cleanup paths:

1. Re-run `analyze` or `analyze-local` for affected builds. `save_analysis()` deletes existing chunks for the build before writing the current chunks.
2. Manually delete old `ci_failure_chunks` where `anchorType=fatal_error_block` or `signature.signatureKey=fatal|fatal error|fatal error||`.
3. Temporarily apply feedback such as `mark_no_owner` or `mark_flaky` to block inheritance for affected failure items.
