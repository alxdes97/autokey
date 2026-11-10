' Launch AutoKey with the venv interpreter and no console window.
' .venv\Scripts\pythonw.exe is a stub that often fails with:
'   No Python at '"...\pythonw.exe"'
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
py = folder & "\.venv\Scripts\python.exe"
script = folder & "\autokey.py"
If Not fso.FileExists(py) Then
    sh.Popup "AutoKey is not set up yet. Double-click run.bat first.", 8, "AutoKey", 16
    WScript.Quit 1
End If
sh.CurrentDirectory = folder
sh.Run """" & py & """ """ & script & """", 0, False
