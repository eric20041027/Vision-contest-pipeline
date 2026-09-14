# Explicit opt-in only. Run from any directory; requires Docker Compose v2 and uv.
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path
$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$projectName = "vcp-pg-" + [Guid]::NewGuid().ToString("N")
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$credentialDir = Join-Path $tempRoot ([IO.Path]::GetRandomFileName())
$savedEnvironment = @{}
$environmentKeys = @(
    "VCP_TEST_PG_PASSWORD", "VCP_TEST_PG_PORT", "VCP_TEST_PG_SERVICE",
    "PGSERVICEFILE", "PGPASSFILE", "PGSERVICE", "PGPASSWORD", "PGHOST",
    "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGOPTIONS", "PYTEST_ADDOPTS"
)
foreach ($key in $environmentKeys) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
}
$exitCode = 1
$composeStarted = $false
$locationPushed = $false
$onWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker Compose is required for PostgreSQL integration tests."
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required for PostgreSQL integration tests."
    }
    if (-not $env:VCP_TEST_PG_PORT) { $env:VCP_TEST_PG_PORT = "55432" }
    $portNumber = 0
    if (-not [int]::TryParse($env:VCP_TEST_PG_PORT, [ref]$portNumber) -or
        $portNumber -lt 1024 -or $portNumber -gt 65535) {
        throw "Invalid integration port."
    }
    if (-not $env:VCP_TEST_PG_PASSWORD) {
        $randomBytes = New-Object byte[] 32
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
        $env:VCP_TEST_PG_PASSWORD = [BitConverter]::ToString($randomBytes).Replace("-", "").ToLowerInvariant()
        [Array]::Clear($randomBytes, 0, $randomBytes.Length)
    }
    # Restrict the directory before writing credentials; avoid unsupported pgpass newlines.
    if ($env:VCP_TEST_PG_PASSWORD.Contains("`n") -or $env:VCP_TEST_PG_PASSWORD.Contains("`r")) {
        throw "Integration password cannot contain a newline."
    }
    New-Item -ItemType Directory -Path $credentialDir | Out-Null
    if ($onWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = Get-Acl -LiteralPath $credentialDir
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $identity, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow"
        )
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $credentialDir -AclObject $acl
    } else {
        & chmod 700 $credentialDir
        if ($LASTEXITCODE -ne 0) { throw "Cannot protect integration credential directory." }
    }
    foreach ($key in @("PGPASSWORD", "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGOPTIONS", "PYTEST_ADDOPTS")) {
        [Environment]::SetEnvironmentVariable($key, $null, "Process")
    }
    $env:PGSERVICEFILE = Join-Path $credentialDir "pg_service.conf"
    $env:PGPASSFILE = Join-Path $credentialDir "pgpass"
    $env:VCP_TEST_PG_SERVICE = "vcp-integration"
    $env:PGSERVICE = $env:VCP_TEST_PG_SERVICE
    $utf8 = New-Object Text.UTF8Encoding($false)
    $serviceText = "[vcp-integration]`nhost=127.0.0.1`nport=$portNumber`nuser=postgres`ndbname=postgres`nsslmode=disable`n"
    [IO.File]::WriteAllText($env:PGSERVICEFILE, $serviceText, $utf8)
    $escapedPassword = $env:VCP_TEST_PG_PASSWORD.Replace('\', '\\').Replace(':', '\:')
    [IO.File]::WriteAllText($env:PGPASSFILE, "127.0.0.1:${portNumber}:*:postgres:${escapedPassword}`n", $utf8)
    $escapedPassword = $null
    if (-not $onWindows) {
        & chmod 600 $env:PGPASSFILE $env:PGSERVICEFILE
        if ($LASTEXITCODE -ne 0) { throw "Cannot protect integration credential files." }
    }
    Push-Location $repoRoot
    $locationPushed = $true
    # Suppress native Compose diagnostics: never risk echoing interpolated environment.
    $composeStarted = $true
    & docker compose --project-name $projectName --file $composeFile up --detach --wait --wait-timeout 120 *> $null
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL integration container did not become healthy." }
    & uv run --frozen --extra postgres pytest "-m" "postgres" tests/integration/provenance -o addopts= --tb=short -q -rs
    $exitCode = $LASTEXITCODE
} catch {
    # Exception text may contain native diagnostics or credential-bearing arguments.
    Write-Host ("PostgreSQL integration harness failed at line {0}. Check Docker Compose and uv availability, port and file permissions." -f $_.InvocationInfo.ScriptLineNumber)
    $exitCode = 1
} finally {
    if ($composeStarted) {
        try {
            & docker compose --project-name $projectName --file $composeFile down --volumes --remove-orphans *> $null
            if ($LASTEXITCODE -ne 0) {
                Write-Host "PostgreSQL integration container/volume cleanup failed."
                $exitCode = 1
            }
        } catch {
            Write-Host "PostgreSQL integration container/volume cleanup failed."
            $exitCode = 1
        }
    }
    try {
        if (Test-Path -LiteralPath $credentialDir) {
            $resolvedCredentialDir = (Resolve-Path -LiteralPath $credentialDir).Path
            if (-not $resolvedCredentialDir.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
                $resolvedCredentialDir -eq $tempRoot -or $resolvedCredentialDir -ne $credentialDir) {
                throw "Unexpected credential cleanup target."
            }
            Remove-Item -LiteralPath $resolvedCredentialDir -Recurse -Force
        }
    } catch {
        Write-Host "Temporary PostgreSQL credential cleanup failed."
        $exitCode = 1
    } finally {
        foreach ($key in $environmentKeys) {
            [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], "Process")
        }
        if ($locationPushed) { Pop-Location }
    }
}
exit $exitCode
