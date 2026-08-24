[CmdletBinding()]
param(
    [string]$PythonExecutable = $env:MESH_PYTHON,
    [ValidateRange(0.1, 60.0)]
    [double]$PollInterval = 1.0
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$platformDir = Join-Path $repoRoot "platform"

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $platformDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $PythonExecutable = $venvPython
    } else {
        $PythonExecutable = (Get-Command python.exe -ErrorAction Stop).Source
    }
}

# 从仓库根运行，使 Worker 子进程调用 src/mesh.py；不在脚本中迁移数据库或探测许可证。
$env:PYTHONPATH = $platformDir
Set-Location -LiteralPath $repoRoot

& $PythonExecutable -m mesh_app.worker --poll-interval $PollInterval
exit $LASTEXITCODE
