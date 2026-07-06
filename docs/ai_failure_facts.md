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

AI history comparison is controlled separately. To validate inherited ownership from AI failure facts, all of these settings must be enabled:

```env
CI_AGENT_AI_FAILURE_FACTS_ENABLED=true
CI_AGENT_AI_HISTORY_COMPARE_ENABLED=true
CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD=0.90
```

If `CI_AGENT_AI_HISTORY_COMPARE_ENABLED` is not true, `aiHistoryPrecheck` stays `null` in the LangSmith initial input even when `failureFacts` are extracted.

Wrapper messages can appear as context, but they must not become the failure identity. If only an outer wrapper is visible, the fact should be marked:

- `historyEligible=false`
- `isGenericWrapper=true`
- low confidence

When AI history comparison is enabled, current non-structured facts can be compared with historical `ci_failure_facts` using AI semantic matching. Inherited ownership is allowed only when the precheck finds `ai_fact_semantic + same_root_cause` with `inheritedOwner.found=true`; generic wrappers and ineligible facts must not inherit.

## Acceptance Command

PowerShell:

```powershell
$env:CI_AGENT_AI_FAILURE_FACTS_ENABLED="true"
$env:CI_AGENT_AI_HISTORY_COMPARE_ENABLED="true"
$env:CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD="0.90"

python -m ci_owner_agent analyze `
  --job services/fx-code-unittest `
  --build 13 `
  --repo fx-code `
  --notify-dry-run
```

Bash:

```bash
export CI_AGENT_AI_FAILURE_FACTS_ENABLED=true
export CI_AGENT_AI_HISTORY_COMPARE_ENABLED=true
export CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD=0.90

python -m ci_owner_agent analyze \
  --job services/fx-code-unittest \
  --build 13 \
  --repo fx-code \
  --notify-dry-run
```
