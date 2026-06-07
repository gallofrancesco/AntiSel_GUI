"""
AntiSEL Dashboard v2.0 — Ethernet TCP
Comunicazione con NUCLEO-H755ZI-Q @ 192.168.1.100:7755

Comandi supportati:
  PING  → PONG
  (altri) → ACK

Requisiti: pip install tkinter (incluso in Python standard)
Avvio: python antisel_dashboard_eth.py
"""

import tkinter as tk
from tkinter import scrolledtext, ttk
import socket
import threading
import time
import queue

HOST = "192.168.1.100"
PORT = 7755
TIMEOUT = 3.0


class AntiSELDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("AntiSEL Dashboard v2.0")
        self.root.geometry("700x520")
        self.root.resizable(True, True)

        self.sock = None
        self.connected = False
        self.rx_queue = queue.Queue()
        self.rx_thread = None

        self._build_ui()
        self.root.after(100, self._poll_rx_queue)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        # ── Barra di stato connessione ──────────────────────────────────
        top = tk.Frame(self.root, bg="#1e1e2e", pady=6)
        top.pack(fill=tk.X)

        tk.Label(top, text="AntiSEL", font=("Courier", 14, "bold"),
                 fg="#cdd6f4", bg="#1e1e2e").pack(side=tk.LEFT, padx=12)

        self.lbl_status = tk.Label(top, text="● DISCONNESSO",
                                   font=("Courier", 10), fg="#f38ba8",
                                   bg="#1e1e2e")
        self.lbl_status.pack(side=tk.LEFT, padx=8)

        tk.Label(top, text=f"{HOST}:{PORT}", font=("Courier", 10, "bold"),
                 fg="#6c7086", bg="#1e1e2e").pack(side=tk.LEFT, padx=4)

        self.btn_conn = tk.Button(top, text="Connetti",
                                  font=("Courier", 10, "bold"), fg="#1e1e2e",
                                  bg="#a6e3a1", relief=tk.FLAT,
                                  padx=10, command=self._toggle_connection)
        self.btn_conn.pack(side=tk.RIGHT, padx=12)

        # ── Metriche live ───────────────────────────────────────────────
        metrics = tk.Frame(self.root, bg="#181825", pady=8)
        metrics.pack(fill=tk.X)

        self.metric_ping = self._metric_card(metrics, "RTT ping", "— ms")
        self.metric_tx   = self._metric_card(metrics, "TX pacchetti", "0")
        self.metric_rx   = self._metric_card(metrics, "RX pacchetti", "0")
        self.metric_err  = self._metric_card(metrics, "Errori", "0")

        self.tx_count = 0
        self.rx_count = 0
        self.err_count = 0

        # ── Log ─────────────────────────────────────────────────────────
        log_frame = tk.Frame(self.root, bg="#181825")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        tk.Label(log_frame, text="Log comunicazione",
                 font=("Courier", 9, "bold"), fg="#6c7086",
                 bg="#181825").pack(anchor=tk.W)

        self.log = scrolledtext.ScrolledText(
            log_frame, height=16, font=("Courier", 10),
            bg="#11111b", fg="#cdd6f4", insertbackground="#cdd6f4",
            relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

        self.log.tag_config("tx",   foreground="#89b4fa")
        self.log.tag_config("rx",   foreground="#a6e3a1")
        self.log.tag_config("info", foreground="#f9e2af")
        self.log.tag_config("err",  foreground="#f38ba8")

        # ── Pannello comandi ────────────────────────────────────────────
        cmd_frame = tk.Frame(self.root, bg="#1e1e2e", pady=6)
        cmd_frame.pack(fill=tk.X, padx=12, pady=(0, 10))

        buttons = [
            ("PING",      "#89b4fa", lambda: self._send_cmd("PING")),
            ("STATUS",    "#cba6f7", lambda: self._send_cmd("STATUS")),
            ("DUT ON",    "#a6e3a1", lambda: self._send_cmd("DUT_ON")),
            ("DUT OFF",   "#f38ba8", lambda: self._send_cmd("DUT_OFF")),
            ("RESET",     "#fab387", lambda: self._send_cmd("RESET")),
            ("Ping loop", "#f9e2af", self._toggle_ping_loop),
        ]

        self.ping_loop_active = False
        self.btn_ping_loop = None

        for label, color, cmd in buttons:
            btn = tk.Button(cmd_frame, text=label,
                            font=("Courier", 9, "bold"), fg="#1e1e2e",
                            bg=color, relief=tk.FLAT, padx=8, pady=4,
                            command=cmd)
            btn.pack(side=tk.LEFT, padx=4)
            if label == "Ping loop":
                self.btn_ping_loop = btn

        # Invio comando manuale
        self.entry_cmd = tk.Entry(cmd_frame, font=("Courier", 10),
                                  bg="#313244", fg="#cdd6f4",
                                  insertbackground="#cdd6f4",
                                  relief=tk.FLAT, width=18)
        self.entry_cmd.pack(side=tk.RIGHT, padx=(8, 0))
        self.entry_cmd.bind("<Return>", lambda e: self._send_manual())

        tk.Button(cmd_frame, text="Invia", font=("Courier", 9, "bold"),
                  fg="#1e1e2e", bg="#cdd6f4", relief=tk.FLAT,
                  padx=8, pady=4,
                  command=self._send_manual).pack(side=tk.RIGHT)

    def _metric_card(self, parent, label, value):
        frame = tk.Frame(parent, bg="#313244", padx=16, pady=6)
        frame.pack(side=tk.LEFT, padx=8)
        tk.Label(frame, text=label, font=("Courier", 8, "bold"),
                 fg="#6c7086", bg="#313244").pack()
        lbl = tk.Label(frame, text=value, font=("Courier", 13, "bold"),
                       fg="#cdd6f4", bg="#313244")
        lbl.pack()
        return lbl

    # ------------------------------------------------------------------ connessione

    def _toggle_connection(self):
        if self.connected:
            self._disconnect()
        else:
            threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        self._log(f"Connessione a {HOST}:{PORT}...", "info")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(TIMEOUT)
            s.connect((HOST, PORT))
            s.settimeout(None)   # ← rimuovi timeout dopo connessione
            self.sock = s
            self.connected = True
            self.root.after(0, self._on_connected)

            self.rx_thread = threading.Thread(
                target=self._rx_loop, daemon=True)
            self.rx_thread.start()

        except Exception as e:
            self.rx_queue.put(("err", f"Connessione fallita: {e}"))

    def _disconnect(self):
        self.ping_loop_active = False
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self._on_disconnected()

    def _on_connected(self):
        self.lbl_status.config(text="● CONNESSO", fg="#a6e3a1")
        self.btn_conn.config(text="Disconnetti", bg="#f38ba8")
        self._log("Connesso.", "info")

    def _on_disconnected(self):
        self.lbl_status.config(text="● DISCONNESSO", fg="#f38ba8")
        self.btn_conn.config(text="Connetti", bg="#a6e3a1")
        self._log("Disconnesso.", "info")

    # ------------------------------------------------------------------ I/O

    def _rx_loop(self):
        try:
            buffer = b""
            while self.connected:
                chunk = self.sock.recv(256)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode().strip()
                    if line:
                        self.rx_queue.put(("rx", line))
        except Exception:
            pass
        finally:
            if self.connected:
                self.rx_queue.put(("err", "Connessione persa."))
                self.connected = False
                self.root.after(0, self._on_disconnected)

    def _send_cmd(self, cmd):
        if not self.connected or not self.sock:
            self._log("Non connesso.", "err")
            return None
        try:
            t0 = time.time()
            self.sock.sendall((cmd + "\r\n").encode())
            self.tx_count += 1
            self.metric_tx.config(text=str(self.tx_count))
            self._log(f"→ {cmd}", "tx")
            return t0
        except Exception as e:
            self.err_count += 1
            self.metric_err.config(text=str(self.err_count))
            self._log(f"Errore TX: {e}", "err")
            return None

    def _send_manual(self):
        cmd = self.entry_cmd.get().strip()
        if cmd:
            self._send_cmd(cmd)
            self.entry_cmd.delete(0, tk.END)

    def _poll_rx_queue(self):
        try:
            while True:
                kind, msg = self.rx_queue.get_nowait()
                if kind == "rx":
                    self.rx_count += 1
                    self.metric_rx.config(text=str(self.rx_count))
                    self._log(f"← {msg}", "rx")
                    if msg == "PONG":
                        pass  # RTT calcolato altrove
                elif kind == "err":
                    self.err_count += 1
                    self.metric_err.config(text=str(self.err_count))
                    self._log(msg, "err")
                elif kind == "rtt":
                    self.metric_ping.config(text=f"{msg:.1f} ms")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_rx_queue)

    # ------------------------------------------------------------------ ping loop

    def _toggle_ping_loop(self):
        if self.ping_loop_active:
            self.ping_loop_active = False
            self.btn_ping_loop.config(text="Ping loop", bg="#f9e2af")
        else:
            self.ping_loop_active = True
            self.btn_ping_loop.config(text="Stop loop", bg="#f38ba8")
            threading.Thread(target=self._ping_loop_thread,
                             daemon=True).start()

    def _ping_loop_thread(self):
        while self.ping_loop_active and self.connected:
            try:
                t0 = time.time()
                self.sock.sendall(b"PING\r\n")
                self.tx_count += 1
                self.root.after(0, lambda: self.metric_tx.config(
                    text=str(self.tx_count)))
                self._log("→ PING", "tx")

            # Aspetta PONG dalla rx_queue invece di leggere il socket
                try:
                    kind, msg = self.rx_queue.get(timeout=3.0)
                    rtt = (time.time() - t0) * 1000
                    self.rx_count += 1
                    ms = rtt
                    self.root.after(0, lambda r=msg, ms=rtt: (
                        self.metric_rx.config(text=str(self.rx_count)),
                        self.metric_ping.config(text=f"{ms:.1f} ms"),
                        self._log(f"← {r}  [{ms:.1f} ms]", "rx")
                    ))
                except queue.Empty:
                    self._log("Ping timeout", "err")

            except Exception as e:
                self.rx_queue.put(("err", f"Ping loop error: {e}"))
                self.ping_loop_active = False
                break
            time.sleep(1.0)
    # ------------------------------------------------------------------ log

    def _log(self, msg, tag="info"):
        ts = time.strftime("%H:%M:%S")
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{ts}] {msg}\n", tag)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = AntiSELDashboard(root)
    root.mainloop()