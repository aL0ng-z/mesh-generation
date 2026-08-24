[CmdletBinding()]
param(
    [string]$BindAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [ValidateRange(0.1, 60.0)]
    [double]$PollInterval = 1.0,
    [switch]$Migrate
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$platformDir = Join-Path $repoRoot "platform"

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$Value)

    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    if ([IO.Path]::IsPathRooted($expanded)) {
        return [IO.Path]::GetFullPath($expanded)
    }
    return [IO.Path]::GetFullPath((Join-Path $repoRoot $expanded))
}

function Import-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolvedPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $lineNumber = 0
    foreach ($rawLine in [IO.File]::ReadAllLines($resolvedPath, [Text.Encoding]::UTF8)) {
        $lineNumber += 1
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }

        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "环境配置格式错误：$resolvedPath 第 $lineNumber 行必须为 KEY=VALUE。"
        }

        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "环境变量名无效：$resolvedPath 第 $lineNumber 行的 '$name'。"
        }

        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2) {
            $doubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
            $singleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
            if ($doubleQuoted -or $singleQuoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
    Write-Host "已加载环境配置：$resolvedPath"
}

function Resolve-PythonExecutable {
    $configured = $env:MESH_PYTHON
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        $repoCandidate = Resolve-RepoPath $configured
        if (Test-Path -LiteralPath $repoCandidate -PathType Leaf) {
            return $repoCandidate
        }
        return (Get-Command $configured -ErrorAction Stop).Source
    }

    $venvPython = Join-Path $platformDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    return (Get-Command python.exe -ErrorAction Stop).Source
}

$rootEnvFile = Join-Path $repoRoot ".env"
if (Test-Path -LiteralPath $rootEnvFile -PathType Leaf) {
    Import-DotEnv $rootEnvFile
}

if ([string]::IsNullOrWhiteSpace($env:MESH_DATA_DIR)) {
    $env:MESH_DATA_DIR = Join-Path $repoRoot "runs\platform-dev"
} else {
    $env:MESH_DATA_DIR = Resolve-RepoPath $env:MESH_DATA_DIR
}
if ([string]::IsNullOrWhiteSpace($env:MESH_DATABASE_PATH)) {
    $env:MESH_DATABASE_PATH = Join-Path $env:MESH_DATA_DIR "mesh.sqlite3"
} else {
    $env:MESH_DATABASE_PATH = Resolve-RepoPath $env:MESH_DATABASE_PATH
}
if ([string]::IsNullOrWhiteSpace($env:MESH_IGG_PATH)) {
    $legacyIggPath = if (-not [string]::IsNullOrWhiteSpace($env:IGG_EXE)) {
        $env:IGG_EXE
    } else {
        $env:IGG_PATH
    }
    if (-not [string]::IsNullOrWhiteSpace($legacyIggPath)) {
        $env:MESH_IGG_PATH = Resolve-RepoPath $legacyIggPath
    }
} else {
    $env:MESH_IGG_PATH = Resolve-RepoPath $env:MESH_IGG_PATH
}

$pythonExecutable = Resolve-PythonExecutable
$env:MESH_PYTHON = $pythonExecutable
$env:PYTHONPATH = $platformDir
$env:PYTHONUNBUFFERED = "1"

$uiIndex = Join-Path $platformDir "ui\dist\index.html"
if (-not (Test-Path -LiteralPath $uiIndex -PathType Leaf)) {
    throw "找不到前端构建产物：$uiIndex。请先在 platform\ui 执行 npm ci 和 npm run build。"
}

$originalLocation = Get-Location
$apiProcess = $null
$workerExitCode = 0
Set-Location -LiteralPath $repoRoot
try {
    if ($Migrate) {
        & $pythonExecutable -m mesh_app.db migrate
        if ($LASTEXITCODE -ne 0) {
            throw "数据库迁移失败，退出码：$LASTEXITCODE"
        }
    }

    & $pythonExecutable -m mesh_app.db check
    if ($LASTEXITCODE -ne 0) {
        throw "数据库版本检查失败。首次安装或代码升级后请执行 .\platform\deploy\run-local.ps1 -Migrate。"
    }

    $logDir = Join-Path $env:MESH_DATA_DIR "logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $apiStdout = Join-Path $logDir "api-$timestamp.log"
    $apiStderr = Join-Path $logDir "api-$timestamp.error.log"
    $apiArguments = @(
        "-m", "uvicorn", "mesh_app.api:app",
        "--host", $BindAddress,
        "--port", $Port.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
    $apiProcess = Start-Process `
        -FilePath $pythonExecutable `
        -ArgumentList $apiArguments `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $apiStdout `
        -RedirectStandardError $apiStderr `
        -WindowStyle Hidden `
        -PassThru

    $browserHost = if ($BindAddress -in @("0.0.0.0", "::")) { "127.0.0.1" } else { $BindAddress }
    $baseUrl = "http://${browserHost}:$Port"
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    $apiReady = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $apiProcess.Refresh()
        if ($apiProcess.HasExited) {
            $details = if (Test-Path -LiteralPath $apiStderr) {
                (Get-Content -LiteralPath $apiStderr -Encoding UTF8 -Tail 20) -join [Environment]::NewLine
            } else {
                "未生成错误日志。"
            }
            throw "API 启动失败，退出码：$($apiProcess.ExitCode)`n$details"
        }
        try {
            Invoke-WebRequest -Uri "$baseUrl/api/health" -UseBasicParsing -TimeoutSec 1 | Out-Null
            $apiReady = $true
            break
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $apiReady) {
        throw "API 在 15 秒内未就绪，请查看日志：$apiStderr"
    }

    Write-Host ""
    Write-Host "网格平台已启动：$baseUrl"
    Write-Host "数据目录：$env:MESH_DATA_DIR"
    Write-Host "API 日志：$apiStdout"
    if ([string]::IsNullOrWhiteSpace($env:MESH_IGG_PATH)) {
        Write-Warning "未配置 MESH_IGG_PATH；网页可访问，但真实网格任务不会执行。"
    } elseif (-not (Test-Path -LiteralPath $env:MESH_IGG_PATH -PathType Leaf)) {
        Write-Warning "MESH_IGG_PATH 指向的文件不存在：$env:MESH_IGG_PATH"
    }
    Write-Host "Worker 正在当前终端运行，按 Ctrl+C 同时停止 API 和 Worker。"
    Write-Host ""

    $pollIntervalText = $PollInterval.ToString([Globalization.CultureInfo]::InvariantCulture)
    & $pythonExecutable -m mesh_app.worker --poll-interval $pollIntervalText
    $workerExitCode = $LASTEXITCODE
} finally {
    if ($null -ne $apiProcess) {
        $apiProcess.Refresh()
        if (-not $apiProcess.HasExited) {
            Stop-Process -Id $apiProcess.Id -ErrorAction SilentlyContinue
            $apiProcess.WaitForExit(5000) | Out-Null
        }
    }
    Set-Location -LiteralPath $originalLocation
}

if ($workerExitCode -ne 0) {
    throw "Worker 已退出，退出码：$workerExitCode"
}
