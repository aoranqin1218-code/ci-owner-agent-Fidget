param(
    [Parameter(Mandatory = $true)]
    [string]$Job,

    [Parameter(Mandatory = $true)]
    [int]$Build,

    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [int]$LogTailLines = 500
)

python -m ci_owner_agent analyze `
    --job $Job `
    --build $Build `
    --repo $Repo `
    --log-tail-lines $LogTailLines `
    --notify

exit $LASTEXITCODE
