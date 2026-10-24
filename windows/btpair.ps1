# Pair / unpair ONE Bluetooth device identified by MAC (classic BR/EDR) via WinRT.
# Usage: powershell -File btpair.ps1 -Action list|pair|unpair -Mac AA:BB:CC:DD:EE:FF
param(
    [Parameter(Mandatory)][ValidateSet('list', 'pair', 'unpair')][string]$Action,
    [string]$Mac = '',
    [int]$ScanSeconds = 40
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
$null = [Windows.Devices.Bluetooth.BluetoothDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$resultType) {
    $t = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
    $null = $t.Wait(-1)
    $t.Result
}

$macKey = ($Mac -replace '[:\-]', '').ToLower()
function Selector($paired) { [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState($paired) }

function Find-Device($paired, $seconds) {
    # Poll FindAllAsync (each call runs a fresh inquiry); WinRT event callbacks into PowerShell are unreliable.
    $end = (Get-Date).AddSeconds($seconds)
    do {
        $all = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync((Selector $paired))) ([Windows.Devices.Enumeration.DeviceInformationCollection])
        foreach ($d in $all) { if (($d.Id -replace '[:\-]', '').ToLower().Contains($macKey)) { return $d } }
    } while ((Get-Date) -lt $end)
    $null
}

switch ($Action) {
    'list' {
        foreach ($p in $true, $false) {
            $all = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync((Selector $p))) ([Windows.Devices.Enumeration.DeviceInformationCollection])
            foreach ($d in $all) { "{0}`t{1}`t{2}" -f $(if ($p) { 'paired' } else { 'unpaired' }), $d.Name, $d.Id }
        }
    }
    'unpair' {
        $d = Find-Device $true 10
        if (-not $d) { 'not paired'; return }
        $r = Await ($d.Pairing.UnpairAsync()) ([Windows.Devices.Enumeration.DeviceUnpairingResult])
        'unpair: ' + $r.Status
    }
    'pair' {
        $d = Find-Device $false $ScanSeconds
        if (-not $d) { 'device not found (is the Pi discoverable?)'; exit 3 }
        'found: {0} [{1}] canPair={2}' -f $d.Name, $d.Id, $d.Pairing.CanPair
        $cp = $d.Pairing.Custom
        # A PowerShell scriptblock cannot run on the thread Windows calls back on, so the handler is a
        # compiled C# method (no WinRT types at compile time) bound to the WinRT delegate type.
        Add-Type -TypeDefinition @'
public class PairBridge {
    public void Handle(object sender, object args) {
        var t = args.GetType();
        System.Console.WriteLine("pairing requested, kind=" + t.GetProperty("PairingKind").GetValue(args, null));
        t.GetMethod("Accept", System.Type.EmptyTypes).Invoke(args, null);
    }
}
'@
        $handlerType = [Windows.Foundation.TypedEventHandler[Windows.Devices.Enumeration.DeviceInformationCustomPairing, Windows.Devices.Enumeration.DevicePairingRequestedEventArgs]]
        $inv = $handlerType.GetMethod('Invoke')
        'delegate signature: ' + (($inv.GetParameters() | ForEach-Object { $_.ParameterType.FullName }) -join ', ')
        # Build the delegate with an expression tree so it matches the WinRT signature exactly.
        $ex = [System.Linq.Expressions.Expression]
        $bridge = New-Object PairBridge
        $ps = [System.Linq.Expressions.ParameterExpression[]]@($inv.GetParameters() | ForEach-Object { $ex::Parameter($_.ParameterType, $_.Name) })
        $args2 = [System.Linq.Expressions.Expression[]]@($ex::Convert($ps[0], [object]), $ex::Convert($ps[1], [object]))
        $call = $ex::Call($ex::Constant($bridge), $bridge.GetType().GetMethod('Handle'), $args2)
        $h = $ex::Lambda($handlerType, $call, $ps).Compile()
        $null = $cp.add_PairingRequested($h)
        $kinds = [Windows.Devices.Enumeration.DevicePairingKinds]'ConfirmOnly, ConfirmPinMatch'
        $r = Await ($cp.PairAsync($kinds, [Windows.Devices.Enumeration.DevicePairingProtectionLevel]::Default)) ([Windows.Devices.Enumeration.DevicePairingResult])
        'pair: ' + $r.Status + ' protection=' + $r.ProtectionLevelUsed
    }
}
