' Digity Core — silent launcher (no console window)
Dim fso, dir, python, script
Set fso = CreateObject("Scripting.FileSystemObject")
dir    = fso.GetParentFolderName(WScript.ScriptFullName)
python = dir & "\python\pythonw.exe"
script = dir & "\main.py"
CreateObject("WScript.Shell").Run """" & python & """ """ & script & """ --app", 0, False
