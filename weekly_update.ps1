# Weekly regulatory-kb-bot refresh: convert any newly published CTU minutes,
# then rebuild the RAG vector index so new content is searchable.
# Registered as a Windows scheduled task (see setup_schedule.ps1).

$PYEXE = "C:\Users\admin\.conda\envs\pdf-tools\python.exe"
$BOTDIR = "C:\Users\admin\OneDrive - NUV Advisors LLP\9_Tools\regulatory-kb-bot"
$LOGFILE = "$BOTDIR\weekly_update.log"

Set-Location $BOTDIR

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Starting weekly update ===" | Tee-Object -FilePath $LOGFILE -Append

& $PYEXE "$BOTDIR\bot.py" 2>&1 | Tee-Object -FilePath $LOGFILE -Append
"bot.py exit code: $LASTEXITCODE" | Tee-Object -FilePath $LOGFILE -Append

& $PYEXE "$BOTDIR\rag\build_index.py" 2>&1 | Tee-Object -FilePath $LOGFILE -Append
"build_index.py exit code: $LASTEXITCODE" | Tee-Object -FilePath $LOGFILE -Append

$healthOutput = & $PYEXE "$BOTDIR\check_health.py" 2>&1
$healthOutput | Tee-Object -FilePath $LOGFILE -Append

if ($healthOutput -match "^ALERT:") {
    $alertText = ($healthOutput -join "; ") -replace "^ALERT:\s*", ""
    msg.exe * "Regulatory KB Bot needs attention: $alertText (see NEEDS_ATTENTION.md in the regulatory-kb-bot folder or the db_cmets_meetings Drive folder)"
}

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Weekly update finished ===" | Tee-Object -FilePath $LOGFILE -Append
