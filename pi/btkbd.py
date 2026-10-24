#!/usr/bin/env python3
"""Bluetooth HID keyboard emulator for BlueZ (classic BR/EDR).

Registers an HID SDP record, listens on L2CAP PSM 17 (control) / 19 (interrupt),
and sends keyboard reports to the connected host. Controlled through the Unix
socket /run/btkbd.sock (JSON lines), see btkbd-send.
"""
import errno
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

ADAPTER_PATH = "/org/bluez/hci0"
PROFILE_PATH = "/org/bluez/btkbd/profile"
AGENT_PATH = "/org/bluez/btkbd/agent"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
CTL_PSM, INT_PSM = 17, 19
SOCK_PATH = "/run/btkbd.sock"
ALIAS = "Raspberry Pi Keyboard"
PAIRING_WINDOW_S = 120

# Boot-protocol compatible keyboard, no report ID: 8-byte input, 5-bit LED output.
REPORT_DESC = bytes.fromhex(
    "05010906a101"          # Usage Page (Generic Desktop), Usage (Keyboard), Collection (Application)
    "050719e029e7"          # Usage Page (Keyboard), Usage Min E0, Usage Max E7
    "150025017501950881" "02"  # 8 modifier bits (Input: Data,Var,Abs)
    "950175088101"          # 1 reserved byte (Input: Const)
    "950575010508190129059102"  # 5 LED bits (Output)
    "950175039101"          # LED padding (Output: Const)
    "95067508150025650507190029658100"  # 6 key bytes (Input: Data,Array)
    "c0"
)

SDP_RECORD = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
 <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>
 <attribute id="0x0004"><sequence>
   <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
   <sequence><uuid value="0x0011"/></sequence></sequence></attribute>
 <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
 <attribute id="0x0006"><sequence><uint16 value="0x656e"/><uint16 value="0x006a"/><uint16 value="0x0100"/></sequence></attribute>
 <attribute id="0x0009"><sequence><sequence><uuid value="0x1124"/><uint16 value="0x0100"/></sequence></sequence></attribute>
 <attribute id="0x000d"><sequence><sequence>
   <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
   <sequence><uuid value="0x0011"/></sequence></sequence></sequence></attribute>
 <attribute id="0x0100"><text value="{name}"/></attribute>
 <attribute id="0x0101"><text value="Keyboard"/></attribute>
 <attribute id="0x0102"><text value="Raspberry Pi"/></attribute>
 <attribute id="0x0200"><uint16 value="0x0111"/></attribute>
 <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
 <attribute id="0x0202"><uint8 value="0x40"/></attribute>
 <attribute id="0x0203"><uint8 value="0x00"/></attribute>
 <attribute id="0x0204"><boolean value="true"/></attribute>
 <attribute id="0x0205"><boolean value="true"/></attribute>
 <attribute id="0x0206"><sequence><sequence><uint8 value="0x22"/><text encoding="hex" value="{desc}"/></sequence></sequence></attribute>
 <attribute id="0x0207"><sequence><sequence><uint16 value="0x0409"/><uint16 value="0x0100"/></sequence></sequence></attribute>
 <attribute id="0x0209"><boolean value="true"/></attribute>
 <attribute id="0x020a"><boolean value="true"/></attribute>
 <attribute id="0x020b"><uint16 value="0x0100"/></attribute>
 <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
 <attribute id="0x020d"><boolean value="false"/></attribute>
 <attribute id="0x020e"><boolean value="true"/></attribute>
</record>
""".format(name=ALIAS, desc=REPORT_DESC.hex())

MODS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08, "win": 0x08, "super": 0x08,
        "rctrl": 0x10, "rshift": 0x20, "altgr": 0x40, "ralt": 0x40, "rgui": 0x80}

NAMED_KEYS = {
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29, "backspace": 0x2A, "tab": 0x2B,
    "space": 0x2C, "capslock": 0x39, "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "menu": 0x65,
}
for _i in range(12):
    NAMED_KEYS["f%d" % (_i + 1)] = 0x3A + _i

# HID usage IDs are layout independent: the host maps them with ITS layout.
# TEXT_LAYOUT describes what the host layout needs to produce each character.
# Both strings are indexed by usage ID - 0x04 (53 entries, 0x04..0x38); \x00 = no printable char.
US_PLAIN = "abcdefghijklmnopqrstuvwxyz1234567890\n\x1b\b\t -=[]\\\x00;'`,./"
US_SHIFT = "ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()" + "\x00" * 5 + "_+{}|\x00:\"~<>?"
assert len(US_PLAIN) == len(US_SHIFT) == 53


def char_to_usage_us(ch):
    """Return (modifier, usage) to type `ch` on a host using a US layout."""
    if ch == "\x00":
        raise ValueError("unsupported character NUL")
    if ch in US_PLAIN:
        i = US_PLAIN.index(ch)
        return 0, _usage_from_index(i)
    if ch in US_SHIFT:
        i = US_SHIFT.index(ch)
        return MODS["shift"], _usage_from_index(i)
    raise ValueError("unsupported character %r" % ch)


def _usage_from_index(i):
    # US_PLAIN order maps 1:1 onto consecutive usages 0x04..0x38 (with 0x32 = non-US # skipped).
    if i < 26:
        return 0x04 + i
    return 0x1E + (i - 26)  # digits, enter, esc, bs, tab, space, punctuation


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Hid:
    def __init__(self):
        self.ctl = None
        self.itr = None
        self.peer = None
        self.lock = threading.Lock()
        self.leds = 0

    def listen(self, psm):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:  # BT_SECURITY (274/4): level 2 = MEDIUM (needs pairing/encryption)
            s.setsockopt(274, 4, struct.pack("BB", 2, 0))
        except OSError as e:
            log("BT_SECURITY not set:", e)
        s.bind(("00:00:00:00:00:00", psm))
        s.listen(1)
        return s

    def serve(self):
        self.lctl = self.listen(CTL_PSM)
        self.litr = self.listen(INT_PSM)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        log("listening on PSM %d/%d" % (CTL_PSM, INT_PSM))

    def _accept_loop(self):
        while True:
            c = None
            try:
                self.lctl.settimeout(None)
                c, caddr = self.lctl.accept()
                self.litr.settimeout(10)
                i, iaddr = self.litr.accept()
            except (OSError, socket.timeout) as e:
                log("accept failed:", e)
                if c:
                    c.close()
                time.sleep(0.5)
                continue
            self._attach(c, i, caddr[0])

    def _attach(self, c, i, addr):
        if addr.upper() not in paired_addrs():
            log("rejecting unpaired peer", addr)
            c.close()
            i.close()
            return
        self.disconnect()
        with self.lock:
            self.ctl, self.itr, self.peer = c, i, addr
        log("host connected:", addr)
        threading.Thread(target=self._reader, args=(c, i), daemon=True).start()

    def connect_out(self, addr):
        c = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        i = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        c.settimeout(15)
        i.settimeout(15)
        c.connect((addr, CTL_PSM))
        i.connect((addr, INT_PSM))
        c.settimeout(None)
        i.settimeout(None)
        self._attach(c, i, addr)

    def disconnect(self):
        with self.lock:
            for s in (self.ctl, self.itr):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
            self.ctl = self.itr = self.peer = None

    def _reader(self, c, i):
        import select
        while True:
            try:
                r, _, _ = select.select([c, i], [], [], 5)
            except (OSError, ValueError):
                break
            for s in r:
                try:
                    data = s.recv(64)
                except OSError:
                    data = b""
                if not data:
                    log("host disconnected")
                    with self.lock:
                        if self.ctl is c:
                            self.ctl = self.itr = self.peer = None
                    for x in (c, i):
                        try:
                            x.close()
                        except OSError:
                            pass
                    return
                if s is c:
                    self._on_control(c, data)
                else:
                    if data[0] == 0xA2 and len(data) >= 2:  # DATA|Output: LED report
                        self.leds = data[-1]

    def _on_control(self, c, data):
        t = data[0] >> 4
        log("control msg type", t, data.hex())
        if t in (5, 7, 9):            # SET_REPORT / SET_PROTOCOL / SET_IDLE
            c.send(b"\x00")           # HANDSHAKE: SUCCESSFUL
        elif t == 4:                  # GET_REPORT
            c.send(b"\xa1" + bytes(8))
        elif t == 6:                  # GET_PROTOCOL -> report protocol
            c.send(b"\xa0\x01")
        elif t == 1 and data[0] & 0x0F == 5:   # VIRTUAL_CABLE_UNPLUG
            self.disconnect()
        else:
            c.send(b"\x03")           # HANDSHAKE: ERR_UNSUPPORTED_REQUEST

    def send_report(self, mod, keys=()):
        keys = list(keys)[:6]
        pkt = bytes([0xA1, mod, 0] + keys + [0] * (6 - len(keys)))
        with self.lock:
            if not self.itr:
                raise RuntimeError("no host connected")
            self.itr.send(pkt)

    def tap(self, mod, usage, hold=0.02, gap=0.02):
        self.send_report(mod, [usage])
        time.sleep(hold)
        self.send_report(0, [])
        time.sleep(gap)


hid = Hid()


def paired_addrs():
    try:
        out = subprocess.run(["bluetoothctl", "devices", "Paired"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return set()
    return {l.split()[1].upper() for l in out.splitlines() if l.startswith("Device ")}


def trusted_addrs():
    try:
        out = subprocess.run(["bluetoothctl", "devices", "Trusted"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return set()
    return {l.split()[1].upper() for l in out.splitlines() if l.startswith("Device ")}


def autoconnect_loop(interval=10):
    """While no host is connected, keep trying to reach the paired+trusted hosts."""
    while True:
        time.sleep(interval)
        if hid.peer:
            continue
        for addr in sorted(trusted_addrs() & paired_addrs()):
            if hid.peer:
                break
            try:
                hid.connect_out(addr)
                log("auto-connected to", addr)
            except OSError:
                pass  # host off / out of range: try again next round


# ---------------------------------------------------------------- BlueZ glue
class Profile(dbus.service.Object):
    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, props):
        log("NewConnection (unexpected, closing fd)", path)
        os.close(fd.take())

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, path):
        pass


class Agent(dbus.service.Object):
    """Just-works agent; only accepts while the pairing window is open."""
    open_until = 0.0

    def _ok(self):
        if time.time() > Agent.open_until:
            raise dbus.exceptions.DBusException("pairing window closed", name="org.bluez.Error.Rejected")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        if str(device).split("dev_")[-1].replace("_", ":").upper() not in paired_addrs():
            self._ok()

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        self._ok()
        log("confirming passkey", passkey, "for", device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        self._ok()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self._ok()
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._ok()
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


bus = None


def adapter_props():
    return dbus.Interface(bus.get_object("org.bluez", ADAPTER_PATH), "org.freedesktop.DBus.Properties")


def set_pairing(on):
    p = adapter_props()
    if on:
        Agent.open_until = time.time() + PAIRING_WINDOW_S
        p.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(PAIRING_WINDOW_S))
        p.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(PAIRING_WINDOW_S))
        p.Set("org.bluez.Adapter1", "Pairable", True)
        p.Set("org.bluez.Adapter1", "Discoverable", True)
    else:
        Agent.open_until = 0.0
        p.Set("org.bluez.Adapter1", "Discoverable", False)
        p.Set("org.bluez.Adapter1", "Pairable", False)


def setup_bluez():
    global bus
    bus = dbus.SystemBus()
    p = adapter_props()
    for attempt in range(15):  # right after a bluetoothd restart the adapter answers org.bluez.Error.Busy
        try:
            p.Set("org.bluez.Adapter1", "Powered", True)
            break
        except dbus.exceptions.DBusException as e:
            if attempt == 14 or "Busy" not in str(e) and "NotReady" not in str(e):
                raise
            time.sleep(1)
    p.Set("org.bluez.Adapter1", "Alias", ALIAS)
    set_pairing(False)

    Profile(bus, PROFILE_PATH)
    pm = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
    pm.RegisterProfile(PROFILE_PATH, HID_UUID, {
        "Role": "server",
        "RequireAuthentication": True,
        "RequireAuthorization": False,
        "AutoConnect": False,
        "ServiceRecord": SDP_RECORD,
    })
    Agent(bus, AGENT_PATH)
    am = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    am.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    am.RequestDefaultAgent(AGENT_PATH)
    log("BlueZ profile + agent registered")


def on_main(fn):
    """Run fn on the GLib main loop (dbus-python is not thread-safe) and wait."""
    box, ev = {}, threading.Event()

    def run():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e
        ev.set()
        return False
    GLib.idle_add(run)
    ev.wait(15)
    if "e" in box:
        raise box["e"]
    return box.get("v")


# ------------------------------------------------------- USB keyboard relay
def _build_usage_map():
    """Linux evdev key code -> HID keyboard usage (positional, like a real keyboard)."""
    from evdev import ecodes as E
    m = {}
    for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        m[getattr(E, "KEY_" + ch)] = 0x04 + i
    for i, ch in enumerate("1234567890"):
        m[getattr(E, "KEY_" + ch)] = 0x1E + i
    names = ["ENTER", "ESC", "BACKSPACE", "TAB", "SPACE", "MINUS", "EQUAL", "LEFTBRACE", "RIGHTBRACE",
             "BACKSLASH", None,  # 0x32 non-US hash: no distinct Linux key, KEY_BACKSLASH covers 0x31
             "SEMICOLON", "APOSTROPHE", "GRAVE", "COMMA", "DOT", "SLASH", "CAPSLOCK"]
    names += ["F%d" % n for n in range(1, 13)]
    names += ["SYSRQ", "SCROLLLOCK", "PAUSE", "INSERT", "HOME", "PAGEUP", "DELETE", "END", "PAGEDOWN",
              "RIGHT", "LEFT", "DOWN", "UP", "NUMLOCK", "KPSLASH", "KPASTERISK", "KPMINUS", "KPPLUS", "KPENTER"]
    names += ["KP%d" % n for n in range(1, 10)] + ["KP0", "KPDOT", "102ND", "COMPOSE"]
    for i, n in enumerate(names):
        if n:
            m[getattr(E, "KEY_" + n)] = 0x28 + i
    # the report descriptor declares usages 0x00..0x65 only
    assert max(m.values()) == 0x65, hex(max(m.values()))
    mods = {E.KEY_LEFTCTRL: 0x01, E.KEY_LEFTSHIFT: 0x02, E.KEY_LEFTALT: 0x04, E.KEY_LEFTMETA: 0x08,
            E.KEY_RIGHTCTRL: 0x10, E.KEY_RIGHTSHIFT: 0x20, E.KEY_RIGHTALT: 0x40, E.KEY_RIGHTMETA: 0x80}
    return m, mods


class Relay:
    """Forwards every USB/other keyboard seen by evdev to the connected Bluetooth host."""

    def __init__(self):
        self.enabled = True
        self.lock = threading.Lock()
        self.devs = {}      # path -> name, for status
        self.grabbed = set()
        self.held = []      # [(path, code)] in press order
        self.sent = 0
        self.usage, self.mods = _build_usage_map()

    def start(self):
        threading.Thread(target=self._scan_loop, daemon=True).start()

    def status(self):
        return {"enabled": self.enabled, "devices": sorted(self.devs.values()),
                "grabbed": len(self.grabbed), "reports_sent": self.sent}

    def _is_keyboard(self, dev):
        from evdev import ecodes as E
        keys = dev.capabilities().get(E.EV_KEY, [])
        return all(k in keys for k in (E.KEY_A, E.KEY_Z, E.KEY_ENTER, E.KEY_SPACE, E.KEY_LEFTCTRL))

    def _scan_loop(self):
        import evdev
        while True:
            try:
                for path in evdev.list_devices():
                    if path in self.devs:
                        continue
                    try:
                        dev = evdev.InputDevice(path)
                        ok = self._is_keyboard(dev)
                    except OSError:
                        continue
                    if not ok:
                        dev.close()
                        continue
                    self.devs[path] = dev.name
                    log("relay: keyboard found:", dev.name, path)
                    threading.Thread(target=self._dev_loop, args=(dev,), daemon=True).start()
            except Exception as e:
                log("relay scan error:", e)
            time.sleep(2)

    def _flush(self):
        mod, keys = 0, []
        for _, code in self.held:
            if code in self.mods:
                mod |= self.mods[code]
            elif len(keys) < 6:
                keys.append(self.usage[code])
        try:
            hid.send_report(mod, keys)
            self.sent += 1
        except (RuntimeError, OSError):
            pass

    def _release_all(self, path=None):
        with self.lock:
            before = len(self.held)
            self.held = [h for h in self.held if path is not None and h[0] != path]
            if before:
                self._flush()

    def _dev_loop(self, dev):
        import select
        from evdev import ecodes as E
        path = dev.path
        grabbed = False
        leds = None
        retry_at, warned = 0.0, False
        try:
            while True:
                want = self.enabled and hid.peer is not None
                if want and not grabbed and time.time() >= retry_at:
                    self._release_all(path)
                    try:
                        dev.grab()
                    except OSError as e:
                        if e.errno != errno.EBUSY:
                            raise
                        if not warned:  # another program already grabbed it
                            log("relay: %s is grabbed by another program (EBUSY); retrying every 5s" % dev.name)
                            warned = True
                        retry_at = time.time() + 5
                    else:
                        grabbed, warned = True, False
                        self.grabbed.add(path)
                        leds = None
                        log("relay: grabbed", dev.name)
                    want = grabbed
                if not want and grabbed:
                    dev.ungrab()
                    grabbed = False
                    self.grabbed.discard(path)
                    self._release_all(path)
                    log("relay: released", dev.name)
                if grabbed and hid.leds != leds:   # mirror host LEDs (NumLock/CapsLock/ScrollLock)
                    leds = hid.leds
                    for bit, led in ((0, E.LED_NUML), (1, E.LED_CAPSL), (2, E.LED_SCROLLL)):
                        try:
                            dev.set_led(led, bool(leds >> bit & 1))
                        except OSError:
                            pass
                r, _, _ = select.select([dev.fd], [], [], 0.5)
                if not r:
                    continue
                for ev in dev.read():
                    if ev.type != E.EV_KEY or ev.value == 2 or not grabbed:
                        continue
                    code = ev.code
                    if code not in self.mods and code not in self.usage:
                        continue
                    with self.lock:
                        item = (path, code)
                        if ev.value == 1 and item not in self.held:
                            self.held.append(item)
                        elif ev.value == 0 and item in self.held:
                            self.held.remove(item)
                        else:
                            continue
                        self._flush()
        except OSError as e:
            log("relay: device gone:", dev.name, e)
        finally:
            self.grabbed.discard(path)
            self._release_all(path)
            self.devs.pop(path, None)
            try:
                dev.close()
            except OSError:
                pass


relay = Relay()


# ------------------------------------------------------------- command server
def handle(cmd):
    c = cmd.get("cmd")
    if c == "status":
        return {"connected": hid.peer, "leds": hid.leds, "paired": sorted(paired_addrs()), "relay": relay.status()}
    if c == "relay":
        if "on" in cmd:
            relay.enabled = bool(cmd["on"])
        return {"relay": relay.status()}
    if c == "pairing":
        on_main(lambda: set_pairing(bool(cmd.get("on", True))))
        return {"pairing": bool(cmd.get("on", True)), "window_s": PAIRING_WINDOW_S}
    if c == "connect":
        hid.connect_out(cmd["addr"])
        return {"connected": hid.peer}
    if c == "disconnect":
        hid.disconnect()
        return {}
    if c == "text":
        for ch in cmd["text"]:
            mod, usage = char_to_usage_us(ch)
            hid.tap(mod, usage)
        return {"sent": len(cmd["text"])}
    if c == "key":
        name = cmd["key"].lower()
        mod = 0
        for m in cmd.get("mods", []):
            mod |= MODS[m.lower()]
        if name in NAMED_KEYS:
            usage = NAMED_KEYS[name]
        elif len(name) == 1:
            m2, usage = char_to_usage_us(name)
            mod |= m2
        else:
            raise ValueError("unknown key " + name)
        hid.tap(mod, usage)
        return {"sent": name}
    if c == "raw":  # [mod, k1..k6] hex, for debugging
        b = bytes.fromhex(cmd["hex"])
        hid.send_report(b[0], list(b[1:]))
        return {}
    raise ValueError("unknown cmd")


def command_server():
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o660)
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


def _serve_conn(conn):
    with conn, conn.makefile("rw") as f:
        for line in f:
            try:
                resp = {"ok": True, **handle(json.loads(line))}
            except Exception as e:
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            f.write(json.dumps(resp) + "\n")
            f.flush()


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    setup_bluez()
    hid.serve()
    threading.Thread(target=command_server, daemon=True).start()
    threading.Thread(target=autoconnect_loop, daemon=True).start()
    relay.start()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
