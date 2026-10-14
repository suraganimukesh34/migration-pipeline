# Checks the app repo for new migration commits and writes a pending skill version if one is due.
# Never adopts a version: a person still reviews and adopts it.
# With -ProposeRepo, a new version is also opened as a draft PR on that repo (merging the PR adopts it).
# Usage: scheduled_run.ps1 -Owner <owner> -Repo <repo> [-ProposeRepo <owner/name>]
param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$ProposeRepo
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
$runArgs = @("run", "--mcp", $Owner, $Repo)
if ($ProposeRepo) {
    # A new version is also put up for review as a PR on this repo, from a clone kept only for that.
    $clone = Join-Path $root (".proposals\" + ($ProposeRepo -split "/")[-1])
    if (-not (Test-Path (Join-Path $clone ".git"))) {
        & git clone "https://github.com/$ProposeRepo.git" $clone 2>&1 | ForEach-Object { "$_" } | Add-Content -Path $log -Encoding utf8
    }
    $runArgs += @("--propose-repo-path", $clone, "--propose-repo", $ProposeRepo)
}
$output = & py -3 pipeline.py @runArgs 2>&1 | ForEach-Object { "$_" }
$output | Add-Content -Path $log -Encoding utf8
"exit=$LASTEXITCODE" | Add-Content -Path $log -Encoding utf8
exit $LASTEXITCODE
