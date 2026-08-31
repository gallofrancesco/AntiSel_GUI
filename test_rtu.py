import sys
sys.path.append("/home/francesco/Documenti/DevRepo/AntiSel_GUI")
from nucleo_client import RtuClient
import time
import queue

client = RtuClient(host="127.0.0.1", port=7756)
client.connect()
time.sleep(2)

print("Connected:", client.connected)
while not client.rx_queue.empty():
    print(client.rx_queue.get_nowait())
client.disconnect()
