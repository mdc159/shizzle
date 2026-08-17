$ErrorActionPreference = 'Stop'

$missing = @()
foreach ($command in @('git', 'gh', 'uv')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        $missing += $command
    }
}
if ($missing.Count -gt 0) {
    throw "Missing required host commands: $($missing -join ', ')"
}
if ([string]::IsNullOrWhiteSpace($env:E2B_API_KEY)) {
    throw 'E2B_API_KEY is missing from this host process. See ENVIRONMENT.md.'
}

Write-Output 'E2B_API_KEY: present (value not displayed)'
Write-Output "GREPTILE_API_KEY: $(if (Test-Path Env:GREPTILE_API_KEY) { 'present' } else { 'not set; optional' })"
$githubLogin = gh api user --jq .login
if ($LASTEXITCODE -ne 0 -or -not $githubLogin) {
    throw 'GitHub CLI authentication failed. Run gh auth login.'
}
Write-Output "GitHub CLI: authenticated as $githubLogin (token not displayed)"
uv run "$PSScriptRoot\tools\e2b_pr_sandbox.py" doctor
if ($LASTEXITCODE -ne 0) {
    throw 'E2B controller doctor failed.'
}
Write-Output 'PR review package preflight: PASS'
