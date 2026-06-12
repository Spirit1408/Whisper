@echo off
rem Removes the app from Windows startup for the current user.
powershell -NoProfile -Command ^
  "Remove-Item ([Environment]::GetFolderPath('Startup') + '\WhisperDictation.lnk') -ErrorAction SilentlyContinue"
echo Autostart removed (if it was installed).
pause
