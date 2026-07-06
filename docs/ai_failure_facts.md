# AI Failure Facts

`find_test_failure_summaries()` no longer falls back to generic fatal wrapper chunks. Docker, BuildKit, Jenkins, make, and shell wrapper messages are too broad to use as deterministic history signatures and can cause unrelated builds to inherit the wrong owner.

Deterministic history similarity remains limited to structured test failures:

- Mocha blocks such as `1) test title`
- Japa / xfail blocks such as `✖ test title`

Non-structured failures are handled by AI failure facts:

- TypeScript compilation failures
- npm install or dependency resolution failures
- Docker build failures
- lint failures
- Jenkins shell step failures
- other build failures without Mocha/Japa blocks

AI failure facts extract the inner root-cause facts from the current build log and provide them to the LangChain agent as current-build context. They are saved in `ci_failure_facts` for audit and later comparison.

This MVP does not perform AI history comparison and does not produce `inherited_failure_owner` from failure facts. The facts only help the agent reason about the current build owner.

Wrapper messages can appear as context, but they must not become the failure identity. If only an outer wrapper is visible, the fact should be marked:

- `historyEligible=false`
- `isGenericWrapper=true`
- low confidence

A later phase will compare current facts with historical facts using AI semantic matching.
