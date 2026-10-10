' Запускает launch.ps1 без мелькающего окна консоли: ярлык вызывает wscript.exe,
' а он стартует PowerShell скрытым окном (0) и не ждёт его завершения (False).
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\launch.ps1""", 0, False
