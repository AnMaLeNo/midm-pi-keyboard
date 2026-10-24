"""Pipeline self-test on the Pi: a virtual USB-like keyboard (uinput) -> btkbd relay -> Bluetooth -> PC."""
import json, socket, sys, time
from evdev import UInput, ecodes as E

def relay_status():
    s = socket.socket(socket.AF_UNIX)
    s.connect("/run/btkbd.sock")
    s.sendall(b'{"cmd": "status"}\n')
    return json.loads(s.makefile().readline())

before = relay_status()["relay"]["grabbed"]
ui = UInput({E.EV_KEY: list(range(1, 128))}, name="btkbd-selftest-kbd")
try:
    deadline = time.time() + 20
    while time.time() < deadline and relay_status()["relay"]["grabbed"] <= before:
        time.sleep(0.25)
    st = relay_status()
    if st["relay"]["grabbed"] <= before:
        print("FAIL: relay never grabbed the virtual keyboard", st)
        sys.exit(1)
    print("relay grabbed the virtual keyboard; sending LCtrl LShift RCtrl RShift")
    for k in (E.KEY_LEFTCTRL, E.KEY_LEFTSHIFT, E.KEY_RIGHTCTRL, E.KEY_RIGHTSHIFT):
        ui.write(E.EV_KEY, k, 1); ui.syn(); time.sleep(0.15)
        ui.write(E.EV_KEY, k, 0); ui.syn(); time.sleep(0.15)
    time.sleep(0.3)
    print("reports_sent:", relay_status()["relay"]["reports_sent"])
finally:
    ui.close()
