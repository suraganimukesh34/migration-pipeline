# Checks the app repo for new migration commits and writes a pending skill version if one is due.
# Never adopts a version: a person still reviews and adopts it.
# Usage: scheduled_run.ps1 -Owner <owner> -Repo <repo>
param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$Repo
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
$log = Join-Path $root "logs\scheduled-run.log"

# Tasks started by the scheduler may not have the user's tokens in their environment; read them from the user scope.
foreach ($name in "GITHUB_TOKEN", "GITHUB_PR_TOKEN") {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value) { [Environment]::SetEnvironmentVariable($name, $value, "Process") }
    }
}

"=== $(Get-Date -Format s) run $Owner/$Repo" | Add-Content -Path $log -Encoding utf8
$output = & py -3 pipeline.py run --mcp $Owner $Repo 2>&1 | ForEach-Object { "$_" }
$output | Add-Content -Path $log -Encoding utf8
"exit=$LASTEXITCODE" | Add-Content -Path $log -Encoding utf8
exit $LASTEXITCODE
