Option Explicit

Dim fileSystem, shell, scriptPath, powerShellPath, command

Set fileSystem = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptPath = fileSystem.BuildPath(fileSystem.GetParentFolderName(WScript.ScriptFullName), "Open Bot Logs.ps1")
If Not fileSystem.FileExists(scriptPath) Then
    MsgBox "Open Bot Logs.ps1 was not found next to this launcher.", vbCritical, "Bot Logs"
    WScript.Quit 1
End If

powerShellPath = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
command = Chr(34) & powerShellPath & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & scriptPath & Chr(34)
shell.Run command, 0, False
