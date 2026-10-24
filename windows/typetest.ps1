# Types on the Pi into OUR OWN test window (WinForms textbox) and reads the result back.
param(
    [Parameter(Mandatory)][string]$PiTarget,                       # e.g. pi@raspberrypi.local
    [string]$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519"
)
$k = $KeyPath
$piScript = Join-Path $PSScriptRoot '../tests/ascii_test.py'
$outF = Join-Path $env:TEMP 'btkbd_out.txt'
$errF = Join-Path $env:TEMP 'btkbd_err.txt'
$Text = -join ((0x20..0x7E) | ForEach-Object { [char]$_ })
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace W -Name N -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, System.UIntPtr extra);
'@

if (-not (Test-Path $piScript)) { "ABORT: missing $piScript"; return }
$form = New-Object System.Windows.Forms.Form
try {
    $form.Text = 'btkbd test (typed by the Raspberry Pi)'; $form.Width = 900; $form.Height = 220; $form.TopMost = $true
    $tb = New-Object System.Windows.Forms.TextBox
    $tb.Multiline = $true; $tb.Dock = 'Fill'; $tb.Font = New-Object System.Drawing.Font('Consolas', 12)
    $form.Controls.Add($tb)
    $t0 = Get-Date
    $script:clicked = $null; $script:firstChar = $null; $script:lastChar = $null
    $tb.Add_MouseDown({ if (-not $script:clicked) { $script:clicked = Get-Date } })
    $form.Add_MouseDown({ if (-not $script:clicked) { $script:clicked = Get-Date } })
    $tb.Add_TextChanged({ $now = Get-Date; if (-not $script:firstChar) { $script:firstChar = $now }; $script:lastChar = $now })
    $form.Show()
    [W.N]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [W.N]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)  # Alt tap lets us take focus
    $null = [W.N]::SetForegroundWindow($form.Handle)
    $form.Activate(); $tb.Focus() | Out-Null
    for ($i = 0; $i -lt 10; $i++) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }

    if ([W.N]::GetForegroundWindow() -ne $form.Handle) { 'ABORT: test window is not in the foreground, nothing sent'; return }
    'Test window has focus - sending keystrokes from the Pi'

    $sshStart = Get-Date
    $psi = Start-Process ssh -ArgumentList @('-i', $k, '-o', 'BatchMode=yes', $PiTarget, 'sudo python3 -') -NoNewWindow -PassThru `
        -RedirectStandardInput $piScript -RedirectStandardOutput $outF -RedirectStandardError $errF
    $deadline = (Get-Date).AddSeconds(40)
    while (-not $psi.HasExited -and (Get-Date) -lt $deadline) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }
    for ($i = 0; $i -lt 10; $i++) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }

    "pi stdout: " + ((Get-Content $outF -Raw) -replace "\s+$", '')
    "pi stderr: " + ((Get-Content $errF -Raw) -replace "\s+$", '')
    "--- text received by Windows ---"
    $tb.Text
    "--- expected ---"
    $Text
    "MATCH: " + ($tb.Text -ceq $Text)
    "--- timing (seconds since window opened) ---"
    "focus confirmed + ssh started : {0:N1}" -f ($sshStart - $t0).TotalSeconds
    "first char received           : {0}" -f $(if ($script:firstChar) { '{0:N1}' -f ($script:firstChar - $t0).TotalSeconds } else { 'never' })
    "last char received            : {0}" -f $(if ($script:lastChar) { '{0:N1}' -f ($script:lastChar - $t0).TotalSeconds } else { 'never' })
    "mouse click in window         : {0}" -f $(if ($script:clicked) { 'YES at {0:N1}s' -f ($script:clicked - $t0).TotalSeconds } else { 'NO' })
    if ($tb.Text -cne $Text) {
        for ($i = 0; $i -lt [Math]::Min($tb.Text.Length, $Text.Length); $i++) {
            if ($tb.Text[$i] -cne $Text[$i]) { "first diff at {0}: sent '{1}' got '{2}'" -f $i, $Text[$i], $tb.Text[$i]; break }
        }
        "lengths: got $($tb.Text.Length), expected $($Text.Length)"
    }
}
finally {
    $form.Close()
    Remove-Item $outF, $errF -ErrorAction SilentlyContinue
}
