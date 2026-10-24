import json, socket, time
s = socket.socket(socket.AF_UNIX)
s.connect("/run/btkbd.sock")
f = s.makefile("rw")

def cmd(d):
    f.write(json.dumps(d) + "\n"); f.flush()
    r = json.loads(f.readline())
    if not r.get("ok"):
        raise SystemExit(r)

# Modifier-only reports (harmless alone): LCtrl, LShift, RCtrl, RShift - press then release.
for mod in (0x01, 0x02, 0x10, 0x20):
    cmd({"cmd": "raw", "hex": "%02x" % mod})
    time.sleep(0.15)
    cmd({"cmd": "raw", "hex": "00"})
    time.sleep(0.15)
print("sent LCtrl LShift RCtrl RShift")
