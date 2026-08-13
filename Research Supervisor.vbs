Option Explicit

Dim shell, files, root, command, argument
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ _
    & root & "\launch-research-supervisor.ps1"""
For Each argument In WScript.Arguments
    command = command & " """ & Replace(argument, Chr(34), Chr(34) & Chr(34)) & """"
Next
WScript.Quit shell.Run(command, 0, True)
