param([switch]$Apply)
$ErrorActionPreference='Stop'
$startup=[IO.Path]::GetFullPath([Environment]::GetFolderPath('Startup'))
$profileRoot=[IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile')).TrimEnd('\')+'\'
if(-not $startup.StartsWith($profileRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Startup folder is outside the current user profile.'}
$pythonw=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\.venv\Scripts\pythonw.exe'))
$entry=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'startup.py'))
foreach($file in @($pythonw,$entry)){if(-not [IO.File]::Exists($file)){throw "Required file missing: $file"}}
$name='Group Chat Monitor.lnk'
$path=[IO.Path]::GetFullPath((Join-Path $startup $name))
if([IO.Path]::GetDirectoryName($path) -ne $startup){throw 'Shortcut is outside the intended startup folder.'}
$arguments='-B "'+$entry+'"'
$shell=New-Object -ComObject WScript.Shell
$exists=Test-Path -LiteralPath $path
if($exists){
    $prior=$shell.CreateShortcut($path)
    if(-not [string]::Equals($prior.TargetPath,$pythonw,[StringComparison]::OrdinalIgnoreCase) -or $prior.Arguments -ne $arguments){
        throw 'An unrelated startup shortcut has the same name; it was not overwritten.'
    }
}
if($Apply -and -not $exists){
    [void][IO.Directory]::CreateDirectory($startup)
    $link=$shell.CreateShortcut($path)
    $link.TargetPath=$pythonw
    $link.Arguments=$arguments
    $link.WorkingDirectory=$PSScriptRoot
    $link.WindowStyle=7
    $link.IconLocation=$pythonw+',0'
    $link.Description='Start the authorized single-group group chat monitor at this Windows user sign-in.'
    $link.Save()
}
$verified=$false
if(Test-Path -LiteralPath $path){
    $actual=$shell.CreateShortcut($path)
    $verified=([string]::Equals($actual.TargetPath,$pythonw,[StringComparison]::OrdinalIgnoreCase) -and $actual.Arguments -eq $arguments -and $actual.WorkingDirectory -eq $PSScriptRoot)
    if(-not $verified){throw 'Monitor shortcut read-back verification failed.'}
}
$report=[pscustomobject]@{Applied=[bool]$Apply;Shortcut=$path;Target=$pythonw;Arguments=$arguments;
    Trigger='current user Windows sign-in';Created=($Apply -and -not $exists);Verified=$verified;
    GroupAndEmailChanged=$false;NewCredentialCreated=$false;SecuritySettingsChanged=$false;
    RebootOrLogoffPerformed=$false;InitializationRetryLimit=6;WechatDataRetrySeconds=30;
    OutlookNotReadyRetrySeconds=300;BootstrapDoesNotRestartLaterUserExit=$true}
if($Apply){$report|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $PSScriptRoot 'monitor-startup-config-result.json') -Encoding UTF8}
$report|ConvertTo-Json -Depth 5 -Compress
