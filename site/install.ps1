#Requires -Version 5.1
<#
.SYNOPSIS
    AgentFleet bootstrap installer for Windows.

.DESCRIPTION
    Usage:
        irm https://agentfleet.wecansync.com/install.ps1 | iex

    Downloads the latest (or a pinned) AgentFleet release zip, verifies its
    sha256 checksum, extracts it under $env:LOCALAPPDATA\AgentFleet, and runs
    the bundle's own bin\install.py directly with Python -- never through the
    bundle's install.ps1 -- so PowerShell execution policy cannot block it.

    Compatible with Windows PowerShell 5.1 and PowerShell 7+.
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $InstallerArgs
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 does not default to TLS 1.2 on older Windows builds;
# PowerShell 7's .NET runtime already negotiates modern TLS, but forcing this
# is harmless there too.
if ($PSVersionTable.PSVersion.Major -lt 6) {
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        # Best-effort; if this fails, the download attempt below will surface
        # the real TLS error.
    }
}

$script:DefaultBaseUrl = 'https://agentfleet.wecansync.com'

function Write-Info {
    param([string] $Message)
    Write-Host "agentfleet: $Message"
}

function Write-Warning2 {
    param([string] $Message)
    Write-Host "agentfleet: warning: $Message"
}

# Never call `exit` here: under `irm ... | iex` it would close the user's
# PowerShell window. A terminating error keeps the window open and still gives
# `powershell -File install.ps1` a non-zero exit code.
function Fail {
    param([string] $Message)
    throw "agentfleet: $Message"
}

function Test-BaseUrl {
    param([string] $Url)
    $uri = $null
    try {
        $uri = [Uri]$Url
    } catch {
        Fail "AGENTFLEET_BASE_URL is not a valid URL: $Url"
    }
    if ($uri.Scheme -eq 'https') {
        return
    }
    if ($uri.Scheme -eq 'http') {
        if ($uri.Host -eq '127.0.0.1' -or $uri.Host -eq 'localhost' -or $uri.Host -eq '::1') {
            return
        }
        Fail "AGENTFLEET_BASE_URL must use https:// (http:// is only allowed for 127.0.0.1/localhost): $Url"
    }
    Fail "AGENTFLEET_BASE_URL must be an http(s) URL: $Url"
}

function Get-BaseUrl {
    $url = $env:AGENTFLEET_BASE_URL
    if ([string]::IsNullOrWhiteSpace($url)) {
        $url = $script:DefaultBaseUrl
    }
    Test-BaseUrl -Url $url
    return $url.TrimEnd('/')
}

function Test-VersionString {
    param([string] $Version)
    if (-not ($Version -match '^[0-9]+\.[0-9]+\.[0-9]+$')) {
        Fail "invalid version string: $Version"
    }
}

function Get-PythonCommand {
    # Try each launcher and keep the first that is Python 3.10+: a bare
    # `python` may be an old install or the Microsoft Store stub.
    $candidates = @(
        @{ Name = 'py'; Prefix = @('-3') },
        @{ Name = 'python3'; Prefix = @() },
        @{ Name = 'python'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        $checkArgs = $candidate.Prefix + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')
        & $command.Source @checkArgs 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Exe = $command.Source; Prefix = $candidate.Prefix }
        }
    }
    Write-Host "agentfleet: Python 3.10 or newer is required but was not found on PATH." -ForegroundColor Red
    Write-Host "agentfleet:   install it from https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "agentfleet:   or: winget install Python.Python.3.12" -ForegroundColor Red
    Fail "Python 3.10+ is required"
}

function Test-Node {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Host "agentfleet: Node.js (>= 18) is required but was not found on PATH." -ForegroundColor Red
        Write-Host "agentfleet:   install it from https://nodejs.org/" -ForegroundColor Red
        Fail "Node.js (>= 18) is required"
    }
    & $node.Source -e 'process.exit(parseInt(process.versions.node.split(".")[0], 10) >= 18 ? 0 : 1)'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "agentfleet: Node.js >= 18 is required (found an older version)." -ForegroundColor Red
        Write-Host "agentfleet:   install a newer Node.js from https://nodejs.org/" -ForegroundColor Red
        Fail "Node.js >= 18 is required"
    }
}

function Test-Claude {
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claude) {
        Write-Warning2 "'claude' (Claude Code) was not found on PATH."
        Write-Info "  install it with: irm https://claude.ai/install.ps1 | iex"
    }
}

function Get-TextFromUrl {
    param([string] $Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing
        return $response.Content
    } catch {
        Fail "failed to fetch $Url ($($_.Exception.Message))"
    }
}

function Get-SumFromSums {
    param([string] $SumsText, [string] $FileName)
    foreach ($line in ($SumsText -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $FileName) {
            return $parts[0].Trim()
        }
    }
    Fail "SHA256SUMS has no entry for $FileName"
}

function Resolve-Release {
    param([string] $BaseUrl)

    $pinned = $env:AGENTFLEET_VERSION
    if (-not [string]::IsNullOrWhiteSpace($pinned)) {
        Test-VersionString -Version $pinned
        $version = $pinned
        $sumsUrl = "$BaseUrl/releases/$version/SHA256SUMS"
        $sumsText = Get-TextFromUrl -Url $sumsUrl
        $zipName = "agentfleet-$version.zip"
        $zipSha256 = Get-SumFromSums -SumsText $sumsText -FileName $zipName
        $zipRel = "releases/$version/$zipName"
        return [pscustomobject]@{
            Version   = $version
            ZipUrl    = "$BaseUrl/$zipRel"
            ZipSha256 = $zipSha256
        }
    }

    $latestUrl = "$BaseUrl/releases/latest.json"
    $latestText = Get-TextFromUrl -Url $latestUrl
    try {
        $latest = $latestText | ConvertFrom-Json
    } catch {
        Fail "latest.json at $latestUrl is not valid JSON"
    }
    if (-not $latest.version) {
        Fail "latest.json at $latestUrl has no 'version' field"
    }
    Test-VersionString -Version $latest.version
    $version = $latest.version
    if (-not $latest.zip) {
        Fail "latest.json at $latestUrl has no 'zip' field"
    }
    $expectedRel = "releases/$version/agentfleet-$version.zip"
    if ($latest.zip -ne $expectedRel) {
        Fail "latest.json 'zip' field ($($latest.zip)) does not match expected path ($expectedRel)"
    }
    if (-not $latest.zip_sha256) {
        Fail "latest.json at $latestUrl has no 'zip_sha256' field"
    }
    return [pscustomobject]@{
        Version   = $version
        ZipUrl    = "$BaseUrl/$($latest.zip)"
        ZipSha256 = $latest.zip_sha256
    }
}

function Get-AgentFleetHome {
    return (Join-Path $env:LOCALAPPDATA 'AgentFleet')
}

function main {
    $pythonCmd = Get-PythonCommand
    Test-Node
    Test-Claude

    $baseUrl = Get-BaseUrl
    $release = Resolve-Release -BaseUrl $baseUrl
    Write-Info "resolved version: $($release.Version)"

    $agentFleetHome = Get-AgentFleetHome
    New-Item -ItemType Directory -Force -Path $agentFleetHome | Out-Null

    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("agentfleet-" + [System.Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

    try {
        $zipPath = Join-Path $tempDir "agentfleet-$($release.Version).zip"
        Write-Info "downloading agentfleet $($release.Version)..."
        try {
            Invoke-WebRequest -Uri $release.ZipUrl -OutFile $zipPath -UseBasicParsing
        } catch {
            Fail "failed to download $($release.ZipUrl) ($($_.Exception.Message))"
        }

        $actualHash = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash
        if ($actualHash.ToLowerInvariant() -ne $release.ZipSha256.ToLowerInvariant()) {
            Write-Host "agentfleet: checksum verification failed for $zipPath" -ForegroundColor Red
            Write-Host "agentfleet:   expected: $($release.ZipSha256)" -ForegroundColor Red
            Write-Host "agentfleet:   actual:   $actualHash" -ForegroundColor Red
            Fail "refusing to install a release that does not match its published checksum"
        }
        Write-Info "checksum verified."

        # Defense in depth: every entry must sit under agentfleet-<version>/
        # with no absolute path, drive letter, or ".." component.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        try {
            $top = "agentfleet-$($release.Version)/"
            foreach ($entry in $archive.Entries) {
                $name = $entry.FullName -replace '\\', '/'
                if (-not $name.StartsWith($top) -or $name.StartsWith('/') -or $name -match '^[A-Za-z]:' -or $name -match '(^|/)\.\.(/|$)') {
                    Fail "release archive contains an unexpected path: $($entry.FullName)"
                }
            }
        } finally {
            $archive.Dispose()
        }

        $stageDir = Join-Path $tempDir 'stage'
        New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
        try {
            Expand-Archive -Path $zipPath -DestinationPath $stageDir -Force
        } catch {
            Fail "failed to extract $zipPath ($($_.Exception.Message))"
        }

        $extractedDir = Join-Path $stageDir "agentfleet-$($release.Version)"
        $installerPath = Join-Path $extractedDir 'bin\install.py'
        if (-not (Test-Path -LiteralPath $installerPath)) {
            Fail "extracted release is missing bin\install.py; refusing to install"
        }

        $releasesDir = Join-Path $agentFleetHome 'releases'
        New-Item -ItemType Directory -Force -Path $releasesDir | Out-Null
        $targetDir = Join-Path $releasesDir $release.Version
        if (Test-Path -LiteralPath $targetDir) {
            Remove-Item -LiteralPath $targetDir -Recurse -Force
        }
        Move-Item -LiteralPath $extractedDir -Destination $targetDir

        $currentPath = Join-Path $agentFleetHome 'current'
        Set-Content -LiteralPath $currentPath -Value $release.Version

        Write-Info "installed to $targetDir"

        $finalInstallerPath = Join-Path $targetDir 'bin\install.py'
        Write-Info "running installer..."
        $extraArgs = @()
        if ($InstallerArgs) { $extraArgs = @($InstallerArgs) }
        $runArgs = $pythonCmd.Prefix + @($finalInstallerPath) + $extraArgs
        & $pythonCmd.Exe @runArgs
        if ($LASTEXITCODE -ne 0) {
            Fail "installer exited with code $LASTEXITCODE"
        }
    } finally {
        if (Test-Path -LiteralPath $tempDir) {
            Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

main
