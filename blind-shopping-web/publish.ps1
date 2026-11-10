<#
Publish this app to GitHub, then finish the Streamlit Cloud deploy in the browser.

Usage:
    powershell -ExecutionPolicy Bypass -File .\publish.ps1
    powershell -ExecutionPolicy Bypass -File .\publish.ps1 -RepoName my-shopping-app

Requires the GitHub CLI (gh): https://cli.github.com/
#>
param(
    [string]$RepoName = "blind-shopping-assistant"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "GitHub CLI (gh) is missing. Install it from https://cli.github.com/ first." -ForegroundColor Yellow
    exit 1
}

gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Signing in to GitHub - a browser window will open..." -ForegroundColor Yellow
    gh auth login --hostname github.com --git-protocol https --web
}

$login = (gh api user --jq .login).Trim()
$email = (gh api user --jq .email).Trim()
if (-not $email -or $email -eq "null") { $email = "$login@users.noreply.github.com" }

if (-not (Test-Path (Join-Path $root ".git"))) {
    git init | Out-Null
    git branch -M main
}
git config user.name $login
git config user.email $email

git add .
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "Blind shopping assistant web app" | Out-Null
}

gh repo view "$login/$RepoName" --json name *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Repository $login/$RepoName already exists - pushing..." -ForegroundColor Green
    git remote remove origin *> $null
    git remote add origin "https://github.com/$login/$RepoName.git"
    git push -u origin main
} else {
    gh repo create $RepoName --public --source . --remote origin --push
}

Write-Host ""
Write-Host "GitHub repo: https://github.com/$login/$RepoName" -ForegroundColor Green
Write-Host ""
Write-Host "Finish the Streamlit deploy:" -ForegroundColor Cyan
Write-Host "  1. Open https://share.streamlit.io and sign in with GitHub."
Write-Host "  2. Create app -> repo '$RepoName' -> branch 'main' -> main file 'app.py'."
Write-Host "  3. Deploy. The public https URL is ready in about two minutes."
Write-Host ""
Write-Host "Every user pastes their own API key in the sidebar; no key is stored in the repo."
