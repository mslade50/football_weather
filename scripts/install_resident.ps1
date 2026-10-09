<#[Creates a named, disabled scheduled task only when -Install is explicitly passed.
Default is a reviewable plan. -Activate separately enables/starts the task. Never
run this script for audit/tests; no credentials are copied into task arguments.]#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedSha,
    [string]$Repository = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'Football Weather Resident Quotes',
    [switch]$Install,
    [switch]$Activate,
    [switch]$Publish
)
$repoPath = (Resolve-Path -LiteralPath $Repository).Path
$pythonExe = (Resolve-Path -LiteralPath $PythonPath).Path
$residentRoot = Join-Path $repoPath 'data\resident'
$arguments = '-m pipeline.resident --sport all --expected-sha ' + $ExpectedSha + ' --root "' + $residentRoot + '"'
if ($Publish) { $arguments += ' --publish' }
$plan = [ordered]@{TaskName=$TaskName;Executable=$pythonExe;Arguments=$arguments;WorkingDirectory=$repoPath;Install=[bool]$Install;Activate=[bool]$Activate;Publish=[bool]$Publish;InitiallyDisabled=$true}
$plan | ConvertTo-Json
if (-not $Install) {
    if ($Activate) { throw '-Activate requires explicit -Install.' }
    return
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Named task already exists; inspect it before replacing ownership.' }
if ($PSCmdlet.ShouldProcess($TaskName, 'Register disabled resident task')) {
    $action = New-ScheduledTaskAction -Execute $pythonExe -Argument $arguments -WorkingDirectory $repoPath
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -Disable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
    Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
    if ($Activate -and $PSCmdlet.ShouldProcess($TaskName, 'Enable and start resident collector')) {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
}
