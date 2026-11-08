# Keeps `gmcli sync --follow` running. Run at logon via Task Scheduler (see the
# setup note in CLAUDE-LOG). If the sync exits (phone offline, connection
# dropped), it waits and starts again. Never sends messages: sync is read-only.
$gmcli = Join-Path $env:USERPROFILE ".local\bin\gmcli.exe"
$logDir = Join-Path $env:USERPROFILE ".local\state\gmcli"
$log = Join-Path $logDir "follow.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wait = 60
while ($true) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$stamp starting sync --follow"
    & $gmcli sync --follow *>> $log
    $code = $LASTEXITCODE
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$stamp sync exited (code $code); retrying in $wait s"
    Start-Sleep -Seconds $wait
    # Back off up to 15 minutes while the connection keeps failing.
    $wait = [Math]::Min($wait * 2, 900)
}
