[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$TaskPrefix = "MeshExperience",
    [string]$RunAsUser = "NT AUTHORITY\SYSTEM",
    [ValidateRange(0, 600)]
    [int]$StartupDelaySeconds = 30
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$apiScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-api.ps1")).Path
$workerScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-worker.ps1")).Path
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$taskNames = @("$TaskPrefix-API", "$TaskPrefix-Worker")

Write-Host "计划创建以下 Windows 启动任务："
Write-Host "  $($taskNames[0]) -> $apiScript"
Write-Host "  $($taskNames[1]) -> $workerScript"
Write-Host "工作目录：$repoRoot"
Write-Host "运行账户：$RunAsUser"
Write-Host "本脚本不会创建防火墙规则。"

if (-not $Apply) {
    Write-Host "当前仅预览，未修改系统。确认路径、运行账户、IGG 与许可证环境后，使用 -Apply 显式安装。"
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "安装启动任务需要管理员权限。请以管理员身份重新运行，并保留 -Apply。"
}

$delay = New-TimeSpan -Seconds $StartupDelaySeconds
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT$([int]$delay.TotalSeconds)S"
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$definitions = @(
    @{ Name = $taskNames[0]; Script = $apiScript },
    @{ Name = $taskNames[1]; Script = $workerScript }
)

foreach ($definition in $definitions) {
    $arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$($definition.Script)`""
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $repoRoot
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
    Register-ScheduledTask -TaskName $definition.Name -InputObject $task -Force | Out-Null
    Write-Host "已安装：$($definition.Name)"
}

Write-Host "启动任务安装完成。防火墙与 Caddy 服务仍需由运维按组织策略单独配置。"
