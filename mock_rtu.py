import socket
import time

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("0.0.0.0", 7756))
s.listen(1)
print("Listening on 7756...")
conn, addr = s.accept()
print("Connected", addr)

try:
    while True:
        data = conn.recv(1024)
        if not data:
            break
        print("Received", data)
        conn.sendall(b"TEMP=85.0 PWM=50.0 STATE=IDLE\r\n")
except Exception as e:
    print(e)
conn.close()
