param(
    [Parameter(Mandatory = $true)]
    [string]$Job,

    [Parameter(Mandatory = $true)]
    [int]$Build,

    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [int]$LogTailLines = 500
)

# AI failure facts history inheritance acceptance example:
# $env:CI_AGENT_AI_FAILURE_FACTS_ENABLED = "true"
# $env:CI_AGENT_AI_HISTORY_COMPARE_ENABLED = "true"
# $env:CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD = "0.90"

python -m ci_owner_agent analyze `
    --job $Job `
    --build $Build `
    --repo $Repo `
    --log-tail-lines $LogTailLines `
    --notify

exit $LASTEXITCODE
