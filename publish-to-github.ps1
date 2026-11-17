# Publish this source folder to a NEW GitHub repository using GitHub CLI.
# Default visibility is private; never commit generated video/audio/transcripts.
param(
    [string]$RepoName = 'video-summary-skill',
    [ValidateSet('private','public')][string]$Visibility = 'private'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git for Windows first.' }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'Install GitHub CLI (gh), then run gh auth login.' }
gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not logged in. Run gh auth login.' }
if (-not (Test-Path '.git')) { git init -b main }
git add SKILL.md README.md AGENTS.example.md LICENSE .gitignore scripts tests references publish-to-github.ps1
if ($LASTEXITCODE -ne 0) { throw 'git add failed' }
$staged = git diff --cached --name-only
if ($staged) {
    git commit -m 'Initial release: YouTube and Bilibili video summary skill'
    if ($LASTEXITCODE -ne 0) { throw 'git commit failed; check git user.name and user.email.' }
}
$privacy = if ($Visibility -eq 'public') { '--public' } else { '--private' }
gh repo create $RepoName $privacy --source . --remote origin --push
if ($LASTEXITCODE -ne 0) { throw 'GitHub publish failed. Check whether the repository already exists or your account has access.' }
