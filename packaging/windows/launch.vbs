' Starts launch.ps1 without a flashing console window: the shortcut runs
' wscript.exe, which starts PowerShell hidden (window style 0) and does not
' wait for it (False). ASCII only: WSH reads .vbs in the ANSI code page.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\launch.ps1""", 0, False
