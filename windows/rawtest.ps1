# Focus-free end-to-end test: the Pi presses LCtrl/LShift/RCtrl/RShift, an invisible window reads them with Raw Input
# (RIDEV_INPUTSINK = receives keys even without focus) together with the originating device path.
param(
    [Parameter(Mandatory)][string]$PiTarget,                       # e.g. pi@raspberrypi.local
    [string]$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519",
    [string]$PiScriptName = 'mod_test.py'      # script in ../tests, run as root on the Pi
)
$k = $KeyPath
$piScript = Join-Path $PSScriptRoot "../tests/$PiScriptName"
$outF = Join-Path $env:TEMP 'btkbd_raw_out.txt'
$errF = Join-Path $env:TEMP 'btkbd_raw_err.txt'

Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

public class RawSink : Form {
    [StructLayout(LayoutKind.Sequential)] struct RAWINPUTDEVICE { public ushort usUsagePage; public ushort usUsage; public uint dwFlags; public IntPtr hwndTarget; }
    [StructLayout(LayoutKind.Sequential)] struct RAWINPUTHEADER { public uint dwType; public uint dwSize; public IntPtr hDevice; public IntPtr wParam; }
    [StructLayout(LayoutKind.Sequential)] struct RAWKEYBOARD { public ushort MakeCode; public ushort Flags; public ushort Reserved; public ushort VKey; public uint Message; public uint ExtraInformation; }
    [DllImport("user32.dll", SetLastError = true)] static extern bool RegisterRawInputDevices(RAWINPUTDEVICE[] p, uint n, uint size);
    [DllImport("user32.dll")] static extern uint GetRawInputData(IntPtr h, uint cmd, IntPtr data, ref uint size, uint hdrSize);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern uint GetRawInputDeviceInfo(IntPtr dev, uint cmd, StringBuilder data, ref uint size);

    public List<string> Captured = new List<string>();
    public bool Registered;

    public RawSink() {
        FormBorderStyle = FormBorderStyle.None; ShowInTaskbar = false; Opacity = 0;
        StartPosition = FormStartPosition.Manual; Location = new System.Drawing.Point(-32000, -32000); Size = new System.Drawing.Size(1, 1);
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override void OnHandleCreated(EventArgs e) {
        base.OnHandleCreated(e);
        var d = new RAWINPUTDEVICE[] { new RAWINPUTDEVICE { usUsagePage = 1, usUsage = 6, dwFlags = 0x100, hwndTarget = Handle } };
        Registered = RegisterRawInputDevices(d, 1, (uint)Marshal.SizeOf(typeof(RAWINPUTDEVICE)));
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x00FF) {  // WM_INPUT
            uint hdr = (uint)Marshal.SizeOf(typeof(RAWINPUTHEADER)), size = 0;
            GetRawInputData(m.LParam, 0x10000003, IntPtr.Zero, ref size, hdr);
            IntPtr buf = Marshal.AllocHGlobal((int)size);
            try {
                if (GetRawInputData(m.LParam, 0x10000003, buf, ref size, hdr) == size) {
                    var h = (RAWINPUTHEADER)Marshal.PtrToStructure(buf, typeof(RAWINPUTHEADER));
                    if (h.dwType == 1) {
                        var kb = (RAWKEYBOARD)Marshal.PtrToStructure(IntPtr.Add(buf, (int)hdr), typeof(RAWKEYBOARD));
                        uint n = 0; GetRawInputDeviceInfo(h.hDevice, 0x20000007, null, ref n);
                        var sb = new StringBuilder((int)n + 1); GetRawInputDeviceInfo(h.hDevice, 0x20000007, sb, ref n);
                        Captured.Add(string.Format("{0}|0x{1:X2}|{2}", sb, kb.VKey, (kb.Flags & 1) == 0 ? "down" : "up"));
                    }
                }
            } finally { Marshal.FreeHGlobal(buf); }
        }
        base.WndProc(ref m);
    }
}
'@
Add-Type -AssemblyName System.Windows.Forms

if (-not (Test-Path $piScript)) { "ABORT: missing $piScript"; return }
$sink = New-Object RawSink
try {
    $sink.Show()
    "raw input registered: " + $sink.Registered
    $pump = { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
    1..10 | ForEach-Object { & $pump }

    $psi = Start-Process ssh -ArgumentList @('-i', $k, '-o', 'BatchMode=yes', $PiTarget, 'sudo python3 -') -NoNewWindow -PassThru `
        -RedirectStandardInput $piScript -RedirectStandardOutput $outF -RedirectStandardError $errF
    $deadline = (Get-Date).AddSeconds(40)
    while (-not $psi.HasExited -and (Get-Date) -lt $deadline) { & $pump }
    1..50 | ForEach-Object { & $pump }   # grace period for late events

    "pi stdout: " + ((Get-Content $outF -Raw) -replace "\s+$", '')
    "pi stderr: " + ((Get-Content $errF -Raw) -replace "\s+$", '')
    $ev = $sink.Captured | ForEach-Object { $p = $_ -split '\|'; [pscustomobject]@{ Device = $p[0]; VK = $p[1]; State = $p[2] } }
    "events received: " + @($ev).Count
    "--- devices that produced keys ---"
    $ev | Group-Object Device | ForEach-Object { "{0}  ({1} events)" -f $_.Name, $_.Count }
    "--- every event received (device | VK | state) ---"
    $ev | ForEach-Object { "{0} | {1} | {2}" -f $_.Device, $_.VK, $_.State }
    $expected = '0x11', '0x10', '0x11', '0x10'   # LCtrl, LShift, RCtrl, RShift (generic VKs in raw input)
    $fromPi = $ev | Where-Object { $_.Device -match '00001124' }
    $gotDown = @($fromPi | Where-Object State -eq 'down' | ForEach-Object VK)
    "--- key-down from the Pi device ---"
    "expected: " + ($expected -join ' ')
    "received: " + ($gotDown -join ' ')
    "RESULT: " + $(if (($gotDown -join ' ') -ceq ($expected -join ' ')) { 'PASS' } else { 'FAIL' })
}
finally {
    $sink.Close()
    Remove-Item $outF, $errF -ErrorAction SilentlyContinue
}
