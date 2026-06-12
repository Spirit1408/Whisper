' Launch the dictation app without a console window (logs go to app.log)
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = appDir
shell.Run """" & appDir & "\.venv\Scripts\pythonw.exe"" """ & appDir & "\src\main.py""", 0, False
