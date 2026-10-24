import json, socket
text = "".join(chr(c) for c in range(0x20, 0x7F))
s = socket.socket(socket.AF_UNIX)
s.connect("/run/btkbd.sock")
s.sendall((json.dumps({"cmd": "text", "text": text}) + "\n").encode())
print(s.makefile().readline().strip())
