param([switch]$Apply,[Parameter(Mandatory=$true)][string]$OutlookExe,[Parameter(Mandatory=$true)][string]$WeixinExe)
$ErrorActionPreference='Stop'
$startup=[IO.Path]::GetFullPath([Environment]::GetFolderPath('Startup'))
$userProfilePath=[IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile')).TrimEnd('\')+'\'
if(-not $startup.StartsWith($userProfilePath,[StringComparison]::OrdinalIgnoreCase)){
    throw 'Resolved startup directory is outside the current user profile.'
}
$apps=@(
    @{Name='Outlook (Classic).lnk';Exe=[IO.Path]::GetFullPath($OutlookExe)},
    @{Name='Weixin.lnk';Exe=[IO.Path]::GetFullPath($WeixinExe)}
)
$shell=New-Object -ComObject WScript.Shell
$results=@()
foreach($app in $apps){
    if(-not [IO.File]::Exists($app.Exe)){throw "App executable is missing: $($app.Exe)"}
    $path=[IO.Path]::GetFullPath((Join-Path $startup $app.Name))
    if([IO.Path]::GetDirectoryName($path) -ne $startup){throw 'Shortcut target is outside the startup directory.'}
    $exists=Test-Path -LiteralPath $path
    if($exists){
        $prior=$shell.CreateShortcut($path)
        if(-not [string]::Equals($prior.TargetPath,$app.Exe,[StringComparison]::OrdinalIgnoreCase) -or $prior.Arguments){
            throw "An unrelated or differently configured shortcut already exists: $path"
        }
    }
    if($Apply -and -not $exists){
        [void][IO.Directory]::CreateDirectory($startup)
        $link=$shell.CreateShortcut($path)
        $link.TargetPath=$app.Exe
        $link.WorkingDirectory=[IO.Path]::GetDirectoryName($app.Exe)
        $link.Arguments=''
        $link.WindowStyle=7
        $link.IconLocation=$app.Exe+',0'
        $link.Description='Start this installed application when the current Windows user signs in.'
        $link.Save()
    }
    $verified=$false
    $windowStyle=$null
    if(Test-Path -LiteralPath $path){
        $actual=$shell.CreateShortcut($path)
        $windowStyle=$actual.WindowStyle
        $verified=([string]::Equals($actual.TargetPath,$app.Exe,[StringComparison]::OrdinalIgnoreCase) -and
                   -not $actual.Arguments -and [IO.File]::Exists($actual.TargetPath))
        if(-not $verified){throw "Shortcut verification failed: $path"}
    }
    $results += [pscustomobject]@{Name=$app.Name;Shortcut=$path;Executable=$app.Exe;
        ExistedBefore=$exists;Created=($Apply -and -not $exists);Verified=$verified;WindowStyle=$windowStyle}
}
$report=[pscustomobject]@{
    Applied=[bool]$Apply;CurrentUserStartupFolder=$startup;Trigger='current user Windows sign-in';
    Items=$results;MonitorStartupAdded=$false;SecuritySettingsChanged=$false;RebootOrLogoffPerformed=$false;
    VerificationScope='shortcut configuration read-back; actual next-sign-in launch not exercised'
}
if($Apply){
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'startup-config-result.json') -Encoding UTF8
}
$report | ConvertTo-Json -Depth 6 -Compress
