[CmdletBinding()]
param(
    [string]$PythonExecutable = $env:MESH_PYTHON,
    [string]$BindAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$platformDir = Join-Path $repoRoot "platform"
$uiIndex = Join-Path $platformDir "ui\dist\index.html"

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $platformDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $PythonExecutable = $venvPython
    } else {
        $PythonExecutable = (Get-Command python.exe -ErrorAction Stop).Source
    }
}

if (-not (Test-Path -LiteralPath $uiIndex -PathType Leaf)) {
    throw "找不到前端构建产物：$uiIndex。请先在 platform\ui 执行 npm ci 和 npm run build。"
}

# 从仓库根运行；platform 提供 mesh_app 包，其初始化会定位 src/ 网格内核。
$env:PYTHONPATH = $platformDir
Set-Location -LiteralPath $repoRoot

& $PythonExecutable -m uvicorn mesh_app.api:app --host $BindAddress --port $Port
exit $LASTEXITCODE
