' run_hidden.vbs - windowless launcher (no console flash).
' Usage:  wscript.exe run_hidden.vbs <exe> [args...]
' Example: wscript.exe run_hidden.vbs powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\...\watchdog.ps1
' All arguments are re-joined; arguments containing spaces are re-quoted.
Set shell = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then WScript.Quit 1
cmd = ""
For Each arg In WScript.Arguments
  If InStr(arg, " ") > 0 Then
    cmd = cmd & " """ & arg & """"
  Else
    cmd = cmd & " " & arg
  End If
Next
cmd = Mid(cmd, 2)
shell.Run cmd, 0, False
WScript.Quit 0
