@echo off
rem Adds the app (windowless mode) to Windows startup for the current user.
rem To remove: run uninstall_autostart.bat or delete the shortcut from shell:startup
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Startup') + '\WhisperDictation.lnk');" ^
  "$lnk.TargetPath = '%~dp0run_hidden.vbs';" ^
  "$lnk.WorkingDirectory = '%~dp0';" ^
  "$lnk.Description = 'Local push-to-talk dictation';" ^
  "$lnk.Save()"
if %errorlevel%==0 (
  echo Autostart installed: the app will start hidden on next login.
) else (
  echo Failed to install autostart.
)
pause
