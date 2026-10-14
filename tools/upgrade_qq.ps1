# Upgrade QQ NT to 9.9.33 (NapCat requires build 40768+)
# Steps: graceful close QQ -> silent install -> verify version
# NOTE: ASCII-only on purpose. Windows PowerShell 5.1 reads BOM-less files as ANSI(GBK),
#       which corrupts non-ASCII string literals and breaks parsing.
$ErrorActionPreference = 'Continue'
$exe = '<NAPCAT_DIR>\downloads\QQ_9.9.33_x64.exe'
$qqExe = 'C:\Program Files\Tencent\QQNT\QQ.exe'
$oldVersion = '9.9.20.37625'

function Get-QqVersion {
    if (Test-Path $qqExe) { return (Get-Item $qqExe).VersionInfo.FileVersion }
    return 'NOT_INSTALLED'
}

Write-Output "=== BEFORE ==="
Write-Output ("QQ version: {0}" -f (Get-QqVersion))

Write-Output ""
Write-Output "=== STEP 1: close QQ gracefully ==="
$procs = Get-Process -Name QQ -ErrorAction SilentlyContinue
if ($procs) {
    Write-Output ("found {0} QQ processes, sending close request" -f $procs.Count)
    foreach ($p in $procs) { try { $null = $p.CloseMainWindow() } catch {} }
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Get-Process -Name QQ -ErrorAction SilentlyContinue)) { break }
    }
    $left = Get-Process -Name QQ -ErrorAction SilentlyContinue
    if ($left) {
        Write-Output ("{0} processes still alive, force killing" -f $left.Count)
        $left | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
}
if (Get-Process -Name QQ -ErrorAction SilentlyContinue) {
    Write-Output "!! QQ still running, install may fail"
} else {
    Write-Output "QQ fully exited"
}

Write-Output ""
Write-Output "=== STEP 2: silent install (UAC prompt will appear, click YES) ==="
foreach ($flag in @('/s', '/S')) {
    Write-Output ("trying switch: {0}" -f $flag)
    try {
        $p = Start-Process -FilePath $exe -ArgumentList $flag -Verb RunAs -PassThru -Wait
        Write-Output ("installer exit code: {0}" -f $p.ExitCode)
    } catch {
        Write-Output ("launch failed: {0}" -f $_.Exception.Message)
        break
    }
    Start-Sleep -Seconds 5
    $v = Get-QqVersion
    Write-Output ("version now: {0}" -f $v)
    if ($v -ne $oldVersion -and $v -ne 'NOT_INSTALLED') { break }
}

Write-Output ""
Write-Output "=== STEP 3: result ==="
$final = Get-QqVersion
Write-Output ("QQ version: {0}" -f $final)
if ($final -match '^9\.9\.33') {
    Write-Output "[ok] upgrade succeeded, satisfies NapCat requirement (build 40768+)"
} else {
    Write-Output "[x] version is not 9.9.33, manual install may be required"
}
Write-Output ""
Write-Output "=== STEP 4: install dir ==="
Get-ChildItem 'C:\Program Files\Tencent\QQNT' -Filter *.exe -ErrorAction SilentlyContinue |
    Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}} |
    Format-Table -AutoSize | Out-String | Write-Output
