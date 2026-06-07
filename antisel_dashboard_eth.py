"""
AntiSEL Dashboard v3.0 — Ethernet TCP
Comunicazione con NUCLEO-H755ZI-Q @ 192.168.1.100:7755

Comandi supportati:
  PING      -> PONG
  STATUS    -> OK STATUS=IDLE
  DAC_GET   -> DAC=<valore>
  DAC_SET N -> DAC_SET=<valore>

Avvio: python antisel_dashboard_v3.py
"""

import tkinter as tk
from tkinter import scrolledtext, ttk
import socket
import threading
import time
import queue
import math

HOST    = "192.168.1.100"
PORT    = 7755
TIMEOUT = 3.0

DAC_MAX_COUNTS = 4095
VREF           = 3.3
VMAX_DEFAULT   = 3.3


def voltage_to_counts(v, vref=VREF):
    return int(max(0, min(DAC_MAX_COUNTS, round(v / vref * DAC_MAX_COUNTS))))


class AntiSELDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("AntiSEL Dashboard v3.0")
        self.root.geometry("760x640")
        self.root.resizable(True, True)

        self.sock             = None
        self.connected        = False
        self.rx_queue         = queue.Queue()
        self.rx_thread        = None
        self.ping_loop_active = False
        self.btn_ping_loop    = None
        self.wave_active      = False
        self.wave_thread      = None

        self._build_ui()
        self.root.after(100, self._poll_rx_queue)

    # ================================================================ UI

    def _build_ui(self):
        # Barra connessione
        top = tk.Frame(self.root, bg="#1e1e2e", pady=6)
        top.pack(fill=tk.X)

        tk.Label(top, text="AntiSEL", font=("Courier", 14, "bold"),
                 fg="#cdd6f4", bg="#1e1e2e").pack(side=tk.LEFT, padx=12)

        self.lbl_status = tk.Label(top, text="● DISCONNESSO",
                                   font=("Courier", 10), fg="#f38ba8",
                                   bg="#1e1e2e")
        self.lbl_status.pack(side=tk.LEFT, padx=8)

        tk.Label(top, text=f"{HOST}:{PORT}", font=("Courier", 10),
                 fg="#6c7086", bg="#1e1e2e").pack(side=tk.LEFT, padx=4)

        self.btn_conn = tk.Button(top, text="Connetti",
                                  font=("Courier", 10, "bold"),
                                  fg="#1e1e2e", bg="#a6e3a1",
                                  relief=tk.FLAT, padx=10,
                                  command=self._toggle_connection)
        self.btn_conn.pack(side=tk.RIGHT, padx=12)

        # Metriche
        metrics = tk.Frame(self.root, bg="#181825", pady=8)
        metrics.pack(fill=tk.X)

        self.metric_ping = self._metric_card(metrics, "RTT ping",     "— ms")
        self.metric_dac  = self._metric_card(metrics, "DAC counts",   "—")
        self.metric_dacv = self._metric_card(metrics, "DAC tensione", "— V")
        self.metric_tx   = self._metric_card(metrics, "TX",           "0")
        self.metric_rx   = self._metric_card(metrics, "RX",           "0")

        self.tx_count = 0
        self.rx_count = 0

        # Notebook
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        tab_log  = tk.Frame(nb, bg="#181825")
        tab_dac  = tk.Frame(nb, bg="#181825")
        tab_wave = tk.Frame(nb, bg="#181825")

        nb.add(tab_log,  text=" Log ")
        nb.add(tab_dac,  text=" DAC ")
        nb.add(tab_wave, text=" Generatore ")

        self._build_log_tab(tab_log)
        self._build_dac_tab(tab_dac)
        self._build_wave_tab(tab_wave)

        # Barra comandi
        cmd_frame = tk.Frame(self.root, bg="#1e1e2e", pady=6)
        cmd_frame.pack(fill=tk.X, padx=12, pady=(0, 8))

        buttons = [
            ("PING",      "#89b4fa", lambda: self._send_cmd("PING")),
            ("STATUS",    "#cba6f7", lambda: self._send_cmd("STATUS")),
            ("DUT ON",    "#a6e3a1", lambda: self._send_cmd("DUT_ON")),
            ("DUT OFF",   "#f38ba8", lambda: self._send_cmd("DUT_OFF")),
            ("RESET",     "#fab387", lambda: self._send_cmd("RESET")),
            ("Ping loop", "#f9e2af", self._toggle_ping_loop),
        ]
        for label, color, cmd in buttons:
            btn = tk.Button(cmd_frame, text=label,
                            font=("Courier", 9, "bold"), fg="#1e1e2e",
                            bg=color, relief=tk.FLAT, padx=8, pady=4,
                            command=cmd)
            btn.pack(side=tk.LEFT, padx=4)
            if label == "Ping loop":
                self.btn_ping_loop = btn

        self.entry_cmd = tk.Entry(cmd_frame, font=("Courier", 10),
                                  bg="#313244", fg="#cdd6f4",
                                  insertbackground="#cdd6f4",
                                  relief=tk.FLAT, width=18)
        self.entry_cmd.pack(side=tk.RIGHT, padx=(8, 0))
        self.entry_cmd.bind("<Return>", lambda e: self._send_manual())

        tk.Button(cmd_frame, text="Invia",
                  font=("Courier", 9, "bold"), fg="#1e1e2e",
                  bg="#cdd6f4", relief=tk.FLAT, padx=8, pady=4,
                  command=self._send_manual).pack(side=tk.RIGHT)

    # ---------------------------------------------------------------- Tab Log
    def _build_log_tab(self, parent):
        tk.Label(parent, text="Log comunicazione",
                 font=("Courier", 9, "bold"), fg="#6c7086",
                 bg="#181825").pack(anchor=tk.W, padx=8, pady=(4, 0))

        self.log = scrolledtext.ScrolledText(
            parent, font=("Courier", 10),
            bg="#11111b", fg="#cdd6f4",
            insertbackground="#cdd6f4",
            relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        self.log.tag_config("tx",   foreground="#89b4fa")
        self.log.tag_config("rx",   foreground="#a6e3a1")
        self.log.tag_config("info", foreground="#f9e2af")
        self.log.tag_config("err",  foreground="#f38ba8")

    # ---------------------------------------------------------------- Tab DAC
    def _build_dac_tab(self, parent):
        frm = tk.Frame(parent, bg="#181825")
        frm.pack(expand=True, pady=20)

        tk.Label(frm, text="Controllo DAC manuale",
                 font=("Courier", 11, "bold"), fg="#cdd6f4",
                 bg="#181825").grid(row=0, column=0, columnspan=3, pady=(0, 14))

        # Slider counts
        tk.Label(frm, text="Counts:", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=1, column=0,
                                                   sticky=tk.E, padx=8)
        self.dac_slider = tk.Scale(frm, from_=0, to=DAC_MAX_COUNTS,
                                   orient=tk.HORIZONTAL, length=320,
                                   bg="#313244", fg="#cdd6f4",
                                   troughcolor="#11111b",
                                   highlightthickness=0,
                                   command=self._on_dac_slider)
        self.dac_slider.set(0)
        self.dac_slider.grid(row=1, column=1, padx=8)

        self.lbl_dac_counts = tk.Label(frm, text="0",
                                        font=("Courier", 10, "bold"),
                                        fg="#cdd6f4", bg="#181825", width=6)
        self.lbl_dac_counts.grid(row=1, column=2)

        # Slider tensione
        tk.Label(frm, text="Tensione:", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=2, column=0,
                                                   sticky=tk.E, padx=8,
                                                   pady=12)
        self.dac_volt_slider = tk.Scale(frm, from_=0.0, to=VREF,
                                         resolution=0.01,
                                         orient=tk.HORIZONTAL, length=320,
                                         bg="#313244", fg="#cdd6f4",
                                         troughcolor="#11111b",
                                         highlightthickness=0,
                                         command=self._on_dac_volt_slider)
        self.dac_volt_slider.set(0.0)
        self.dac_volt_slider.grid(row=2, column=1, padx=8)

        self.lbl_dac_volt = tk.Label(frm, text="0.00 V",
                                      font=("Courier", 10, "bold"),
                                      fg="#a6e3a1", bg="#181825", width=8)
        self.lbl_dac_volt.grid(row=2, column=2)

        # Pulsanti preset
        btn_row = tk.Frame(frm, bg="#181825")
        btn_row.grid(row=3, column=0, columnspan=3, pady=16)

        for label, val in [("0 V", 0.0), ("1.0 V", 1.0),
                            ("1.65 V", 1.65), ("3.3 V", 3.3)]:
            tk.Button(btn_row, text=label,
                      font=("Courier", 9, "bold"), fg="#1e1e2e",
                      bg="#89b4fa", relief=tk.FLAT, padx=10, pady=4,
                      command=lambda v=val: self._set_dac_voltage(v)
                      ).pack(side=tk.LEFT, padx=6)

        tk.Button(btn_row, text="DAC_GET",
                  font=("Courier", 9, "bold"), fg="#1e1e2e",
                  bg="#cba6f7", relief=tk.FLAT, padx=10, pady=4,
                  command=lambda: self._send_cmd("DAC_GET")
                  ).pack(side=tk.LEFT, padx=6)

    def _on_dac_slider(self, val):
        counts = int(float(val))
        volts  = counts / DAC_MAX_COUNTS * VREF
        self.lbl_dac_counts.config(text=str(counts))
        self.lbl_dac_volt.config(text=f"{volts:.2f} V")
        self.dac_volt_slider.set(round(volts, 2))
        self._send_cmd(f"DAC_SET {counts}")

    def _on_dac_volt_slider(self, val):
        volts  = float(val)
        counts = voltage_to_counts(volts)
        self.lbl_dac_counts.config(text=str(counts))
        self.lbl_dac_volt.config(text=f"{volts:.2f} V")
        self.dac_slider.set(counts)
        self._send_cmd(f"DAC_SET {counts}")

    def _set_dac_voltage(self, v):
        self.dac_volt_slider.set(round(v, 2))

    # ----------------------------------------------------------- Tab Generatore
    def _build_wave_tab(self, parent):
        frm = tk.Frame(parent, bg="#181825")
        frm.pack(expand=True, pady=12, padx=20)

        tk.Label(frm, text="Generatore forme d'onda",
                 font=("Courier", 11, "bold"), fg="#cdd6f4",
                 bg="#181825").grid(row=0, column=0, columnspan=4, pady=(0, 14))

        # Tipo forma d'onda
        tk.Label(frm, text="Forma:", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=1, column=0,
                                                   sticky=tk.E, padx=8)
        self.wave_type = tk.StringVar(value="Sinusoidale")
        for i, wt in enumerate(["Sinusoidale", "Quadra", "Triangolare"]):
            tk.Radiobutton(frm, text=wt, variable=self.wave_type, value=wt,
                           font=("Courier", 9, "bold"),
                           fg="#cdd6f4", bg="#181825",
                           selectcolor="#313244",
                           activebackground="#181825"
                           ).grid(row=1, column=1+i, sticky=tk.W, padx=4)

        # Frequenza
        tk.Label(frm, text="Frequenza (Hz):", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=2, column=0,
                                                   sticky=tk.E, padx=8,
                                                   pady=10)
        self.wave_freq = tk.Scale(frm, from_=0.1, to=10.0, resolution=0.1,
                                   orient=tk.HORIZONTAL, length=280,
                                   bg="#313244", fg="#cdd6f4",
                                   troughcolor="#11111b",
                                   highlightthickness=0,
                                   command=self._on_freq_change)
        self.wave_freq.set(1.0)
        self.wave_freq.grid(row=2, column=1, columnspan=2, sticky=tk.W)

        self.lbl_wave_info = tk.Label(frm, text="1.0 Hz  |  T=1.000 s",
                                       font=("Courier", 9, "bold"),
                                       fg="#f9e2af", bg="#181825", width=20)
        self.lbl_wave_info.grid(row=2, column=3, padx=8)

        # Tensione massima
        tk.Label(frm, text=f"V max (0–{VREF} V):", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=3, column=0,
                                                   sticky=tk.E, padx=8,
                                                   pady=10)
        self.wave_vmax = tk.Scale(frm, from_=0.1, to=VREF, resolution=0.05,
                                   orient=tk.HORIZONTAL, length=280,
                                   bg="#313244", fg="#cdd6f4",
                                   troughcolor="#11111b",
                                   highlightthickness=0,
                                   command=lambda v: self.lbl_wave_vmax.config(
                                       text=f"{float(v):.2f} V"))
        self.wave_vmax.set(VREF)
        self.wave_vmax.grid(row=3, column=1, columnspan=2, sticky=tk.W)

        self.lbl_wave_vmax = tk.Label(frm, text=f"{VREF:.2f} V",
                                       font=("Courier", 9, "bold"),
                                       fg="#a6e3a1", bg="#181825", width=8)
        self.lbl_wave_vmax.grid(row=3, column=3, padx=8)

        # Offset DC
        tk.Label(frm, text="Offset DC (V):", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=4, column=0,
                                                   sticky=tk.E, padx=8,
                                                   pady=10)
        self.wave_offset = tk.Scale(frm, from_=0.0, to=VREF, resolution=0.05,
                                     orient=tk.HORIZONTAL, length=280,
                                     bg="#313244", fg="#cdd6f4",
                                     troughcolor="#11111b",
                                     highlightthickness=0,
                                     command=lambda v: self.lbl_wave_offset.config(
                                         text=f"{float(v):.2f} V"))
        self.wave_offset.set(0.0)
        self.wave_offset.grid(row=4, column=1, columnspan=2, sticky=tk.W)

        self.lbl_wave_offset = tk.Label(frm, text="0.00 V",
                                         font=("Courier", 9, "bold"),
                                         fg="#cdd6f4", bg="#181825", width=8)
        self.lbl_wave_offset.grid(row=4, column=3, padx=8)

        # Campioni per periodo
        tk.Label(frm, text="Campioni/periodo:", font=("Courier", 9, "bold"),
                 fg="#6c7086", bg="#181825").grid(row=5, column=0,
                                                   sticky=tk.E, padx=8,
                                                   pady=10)
        self.wave_samples = tk.Scale(frm, from_=8, to=100, resolution=1,
                                      orient=tk.HORIZONTAL, length=280,
                                      bg="#313244", fg="#cdd6f4",
                                      troughcolor="#11111b",
                                      highlightthickness=0,
                                      command=lambda v: self.lbl_wave_samples.config(
                                          text=str(int(float(v)))))
        self.wave_samples.set(32)
        self.wave_samples.grid(row=5, column=1, columnspan=2, sticky=tk.W)

        self.lbl_wave_samples = tk.Label(frm, text="32",
                                          font=("Courier", 9, "bold"),
                                          fg="#cdd6f4", bg="#181825", width=8)
        self.lbl_wave_samples.grid(row=5, column=3, padx=8)

        # Pulsanti
        btn_row = tk.Frame(frm, bg="#181825")
        btn_row.grid(row=6, column=0, columnspan=4, pady=18)

        self.btn_wave_start = tk.Button(
            btn_row, text="▶  Avvia",
            font=("Courier", 10, "bold"), fg="#1e1e2e",
            bg="#a6e3a1", relief=tk.FLAT, padx=16, pady=6,
            command=self._toggle_wave)
        self.btn_wave_start.pack(side=tk.LEFT, padx=8)

        self.lbl_wave_status = tk.Label(btn_row, text="Generatore fermo",
                                         font=("Courier", 9, "bold"),
                                         fg="#6c7086", bg="#181825")
        self.lbl_wave_status.pack(side=tk.LEFT, padx=12)

    def _on_freq_change(self, val):
        f = float(val)
        self.lbl_wave_info.config(text=f"{f:.1f} Hz  |  T={1/f:.3f} s")

    # ---------------------------------------------------------- generatore loop
    def _toggle_wave(self):
        if self.wave_active:
            self.wave_active = False
            self.btn_wave_start.config(text="▶  Avvia", bg="#a6e3a1")
            self.lbl_wave_status.config(text="Generatore fermo", fg="#6c7086")
        else:
            if not self.connected:
                self._log("Non connesso.", "err")
                return
            self.wave_active = True
            self.btn_wave_start.config(text="■  Stop", bg="#f38ba8")
            self.lbl_wave_status.config(text="Generatore attivo", fg="#a6e3a1")
            self.wave_thread = threading.Thread(
                target=self._wave_loop, daemon=True)
            self.wave_thread.start()

    def _wave_loop(self):
        phase = 0.0
        while self.wave_active and self.connected:
            freq    = self.wave_freq.get()
            vmax    = self.wave_vmax.get()
            offset  = self.wave_offset.get()
            samples = int(self.wave_samples.get())
            wtype   = self.wave_type.get()

            dt = 1.0 / (freq * samples)

            # Valore normalizzato in [0, 1]
            if wtype == "Sinusoidale":
                norm = (math.sin(phase) + 1) / 2
            elif wtype == "Quadra":
                norm = 1.0 if math.sin(phase) >= 0 else 0.0
            else:  # Triangolare
                t = phase / (2 * math.pi)
                norm = 2 * abs(t - math.floor(t + 0.5))

            # Tensione con clamp a [0, VREF]
            v = offset + vmax * norm
            v = max(0.0, min(VREF, v))
            counts = voltage_to_counts(v)

            self._send_cmd(f"DAC_SET {counts}")

            self.root.after(0, lambda vv=v, cc=counts: (
                self.metric_dac.config(text=str(cc)),
                self.metric_dacv.config(text=f"{vv:.2f} V")
            ))

            phase += 2 * math.pi / samples
            if phase >= 2 * math.pi:
                phase -= 2 * math.pi

            time.sleep(dt)

        # Azzera DAC allo stop
        self._send_cmd("DAC_SET 0")

    # ============================================================ connessione

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
            s.settimeout(None)
            self.sock = s
            self.connected = True
            self.root.after(0, self._on_connected)
            self.rx_thread = threading.Thread(
                target=self._rx_loop, daemon=True)
            self.rx_thread.start()
        except Exception as e:
            self.rx_queue.put(("err", f"Connessione fallita: {e}"))

    def _disconnect(self):
        self.wave_active      = False
        self.ping_loop_active = False
        self.connected        = False
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
        self.btn_wave_start.config(text="▶  Avvia", bg="#a6e3a1")
        self.lbl_wave_status.config(text="Generatore fermo", fg="#6c7086")
        self._log("Disconnesso.", "info")

    # ================================================================== I/O

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
            return
        try:
            self.sock.sendall((cmd + "\r\n").encode())
            self.tx_count += 1
            self.root.after(0, lambda: self.metric_tx.config(
                text=str(self.tx_count)))
            # Non loggare DAC_SET durante generatore per non intasare
            if not cmd.startswith("DAC_SET"):
                self._log(f"-> {cmd}", "tx")
        except Exception as e:
            self._log(f"Errore TX: {e}", "err")

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
                    if msg.startswith("DAC="):
                        try:
                            counts = int(msg.split("=")[1])
                            v = counts / DAC_MAX_COUNTS * VREF
                            self.metric_dac.config(text=str(counts))
                            self.metric_dacv.config(text=f"{v:.2f} V")
                        except Exception:
                            pass
                    # Non loggare risposte DAC_SET durante generatore
                    if not (self.wave_active and msg.startswith("DAC_SET")):
                        self._log(f"<- {msg}", "rx")
                elif kind == "err":
                    self._log(msg, "err")
                elif kind == "rtt":
                    self.metric_ping.config(text=f"{msg:.1f} ms")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_rx_queue)

    # ============================================================ ping loop

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
                self._log("-> PING", "tx")

                kind, msg = self.rx_queue.get(timeout=3.0)
                rtt = (time.time() - t0) * 1000
                self.rx_count += 1
                self.root.after(0, lambda r=msg, ms=rtt: (
                    self.metric_rx.config(text=str(self.rx_count)),
                    self.metric_ping.config(text=f"{ms:.1f} ms"),
                    self._log(f"<- {r}  [{ms:.1f} ms]", "rx")
                ))
            except Exception as e:
                self.rx_queue.put(("err", f"Ping loop error: {e}"))
                self.ping_loop_active = False
                break
            time.sleep(1.0)

    # ================================================================== log

    def _log(self, msg, tag="info"):
        ts = time.strftime("%H:%M:%S")
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{ts}] {msg}\n", tag)
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _metric_card(self, parent, label, value):
        frame = tk.Frame(parent, bg="#313244", padx=14, pady=6)
        frame.pack(side=tk.LEFT, padx=6)
        tk.Label(frame, text=label, font=("Courier", 8, "bold"),
                 fg="#6c7086", bg="#313244").pack()
        lbl = tk.Label(frame, text=value, font=("Courier", 12, "bold"),
                       fg="#cdd6f4", bg="#313244")
        lbl.pack()
        return lbl


# =============================================================================

if __name__ == "__main__":
    root = tk.Tk()
    app  = AntiSELDashboard(root)
    root.mainloop()