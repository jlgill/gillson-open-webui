param(
    [ValidateSet('guided', 'execute')]
    [string]$Mode = 'guided',
    [string]$ComposeFile = 'docker-compose.prod.yaml',
    [string]$WebUiService = 'open-webui',
    [string]$PostgresContainer = 'owui-postgres',
    [string]$DbName = 'openwebui',
    [string]$DbUser = 'postgres',
    [string]$ImageRef = 'ghcr.io/jlgill/gillson-open-webui:latest',
    [string]$Branch = 'main',
    [switch]$SkipForkSync,
    [switch]$SkipGhActionsCheck,
    [string]$BackupDir = '.'
)

$ErrorActionPreference = 'Stop'

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-Command {
    param([string]$CommandName)
    if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $CommandName"
    }
}

function Run-Or-Print {
    param([string]$Command)
    if ($Mode -eq 'guided') {
        Write-Host "[guided] $Command" -ForegroundColor Yellow
    } else {
        Write-Host "[execute] $Command" -ForegroundColor Green
        Invoke-Expression $Command
    }
}

function Run-And-Capture {
    param([string]$Command)
    if ($Mode -eq 'guided') {
        Write-Host "[guided] $Command" -ForegroundColor Yellow
        return ''
    }
    return (Invoke-Expression $Command | Out-String)
}

function Resolve-PostgresContainer {
    param([string]$PreferredName)

    if ($Mode -eq 'guided') {
        return $PreferredName
    }

    $exact = docker ps --format "{{.Names}}" | Where-Object { $_ -eq $PreferredName }
    if ($exact) {
        return $PreferredName
    }

    $fallback = docker ps --format "{{.Names}}" | Where-Object { $_ -eq 'postgres' }
    if ($fallback) {
        Write-Host "Preferred container '$PreferredName' not found. Using 'postgres'." -ForegroundColor Yellow
        return 'postgres'
    }

    throw "Could not find PostgreSQL container '$PreferredName' or fallback 'postgres'."
}

function Assert-FileNonZero {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "Backup file not found: $Path"
    }

    $len = (Get-Item $Path).Length
    if ($len -le 0) {
        throw "Backup file is empty: $Path"
    }

    return $len
}

function Test-RepoDirty {
    $status = git status --porcelain
    return -not [string]::IsNullOrWhiteSpace(($status | Out-String))
}

$summary = [ordered]@{
    Mode = $Mode
    ComposeFile = $ComposeFile
    WebUiService = $WebUiService
    PostgresContainer = $PostgresContainer
    DbName = $DbName
    DbUser = $DbUser
    ImageRef = $ImageRef
    BackupFile = ''
    BackupBytes = 0
    MigrationsDetected = $false
    HealthOutput = ''
    Notes = @()
}

Write-Step 'Preflight checks'
Assert-Command docker
Assert-Command git
if (-not (Test-Path $ComposeFile)) {
    throw "Compose file does not exist: $ComposeFile"
}

Write-Step 'Optional fork sync with upstream'
if (-not $SkipForkSync) {
    $hasUpstream = (git remote | Where-Object { $_ -eq 'upstream' })
    if (-not $hasUpstream) {
        Run-Or-Print 'git remote add upstream https://github.com/open-webui/open-webui.git'
    }

    $stashed = $false
    if ($Mode -eq 'execute' -and (Test-RepoDirty)) {
        Run-Or-Print 'git stash'
        $stashed = $true
    }

    Run-Or-Print 'git fetch upstream'
    Run-Or-Print "git checkout $Branch"
    Run-Or-Print "git merge upstream/$Branch"
    Run-Or-Print "git push origin $Branch"

    if ($stashed) {
        Run-Or-Print 'git stash pop'
    }
} else {
    $summary.Notes += 'Fork sync skipped by request.'
}

Write-Step 'Resolve PostgreSQL container name'
$PostgresContainer = Resolve-PostgresContainer -PreferredName $PostgresContainer
$summary.PostgresContainer = $PostgresContainer

Write-Step 'Create and verify database backup'
if (-not (Test-Path $BackupDir)) {
    Run-Or-Print "New-Item -ItemType Directory -Force -Path '$BackupDir' | Out-Null"
}

$date = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$backupFile = Join-Path $BackupDir "openwebui_backup_$date.sql"
$summary.BackupFile = $backupFile

$backupCommand = "docker exec -t $PostgresContainer pg_dump -U $DbUser $DbName > '$backupFile'"
Run-Or-Print $backupCommand

if ($Mode -eq 'execute') {
    $backupBytes = Assert-FileNonZero -Path $backupFile
    $summary.BackupBytes = $backupBytes
}

Write-Step 'Custom image check'
if (-not $SkipGhActionsCheck) {
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        $wfCmd = 'gh run list --workflow docker-build.yaml --limit 1 --json status,conclusion,displayTitle'
        $wf = Run-And-Capture $wfCmd
        if ($Mode -eq 'execute' -and -not [string]::IsNullOrWhiteSpace($wf)) {
            Write-Host "Latest workflow run: $wf"
        }
    } else {
        $summary.Notes += 'GitHub CLI not found; workflow check skipped.'
    }
}
Run-Or-Print "docker manifest inspect $ImageRef"

Write-Step 'Upgrade runtime with Docker Compose'
Run-Or-Print "docker compose -f $ComposeFile down"
Run-Or-Print "docker compose -f $ComposeFile pull"
Run-Or-Print "docker compose -f $ComposeFile up -d"

Write-Step 'Migration and health checks'
$migrationLogs = Run-And-Capture "docker compose -f $ComposeFile logs $WebUiService --tail 200"
if ($Mode -eq 'execute') {
    $summary.MigrationsDetected = ($migrationLogs -match 'alembic.runtime.migration' -or $migrationLogs -match 'Running upgrade')
    if (-not $summary.MigrationsDetected) {
        $summary.Notes += 'No Alembic migration lines detected in recent logs.'
    }
}

$healthOutput = Run-And-Capture 'docker ps --format "table {{.Names}}`t{{.Image}}`t{{.Status}}"'
$summary.HealthOutput = $healthOutput

Run-Or-Print "docker exec -i $PostgresContainer pg_isready -U $DbUser -d $DbName"

Write-Step 'Final summary'
Write-Host "Mode: $($summary.Mode)"
Write-Host "Compose file: $($summary.ComposeFile)"
Write-Host "Postgres container: $($summary.PostgresContainer)"
Write-Host "Backup file: $($summary.BackupFile)"
if ($summary.BackupBytes -gt 0) {
    Write-Host "Backup bytes: $($summary.BackupBytes)"
}
Write-Host "Migrations detected: $($summary.MigrationsDetected)"
if (-not [string]::IsNullOrWhiteSpace($summary.HealthOutput)) {
    Write-Host "Container health:"
    Write-Host $summary.HealthOutput
}
if ($summary.Notes.Count -gt 0) {
    Write-Host 'Notes:'
    $summary.Notes | ForEach-Object { Write-Host "- $_" }
}

if ($Mode -eq 'guided') {
    Write-Host "\nGuided mode completed. No commands were executed." -ForegroundColor Yellow
} else {
    Write-Host "\nExecute mode completed." -ForegroundColor Green
}
