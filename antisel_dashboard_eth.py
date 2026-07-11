"""
AntiSEL Dashboard v4.2 — Ethernet TCP (CustomTkinter UI)
Comunicazione con NUCLEO-H755ZI-Q @ 192.168.1.100:7755

v4.1:
- Scala ADC corretta a 16 bit (il firmware usa ADC_RESOLUTION_16B)
- Parsing traccia allineato al firmware: header TRACE_START con metadati
  (FS, N, THOLD, TON, DAC, TICK) e righe "indice,valore"
- Log 10 Hz salvato su CSV con timestamp (R-08)
- Contatori SEL/HCE e flag FRESH dal firmware
- Slider I_TH con debounce (un solo DAC_SET per regolazione)

v4.2:
- Soglie precaricate TH_LOAD/TH_SELECT (spec §8.2)
- Log eventi/override manuale su CSV con timestamp (spec §5.3)
- Nomenclatura file <DUT_id>_<LET>_<run_id>_<timestamp> (spec §6.3)
"""

import tkinter as tk
import customtkinter as ctk
import socket
import threading
import time
import queue
import math

HOST    = "192.168.1.100"
PORT    = 7755
TIMEOUT = 3.0

DAC_MAX_COUNTS = 4095    # DAC della Nucleo: 12 bit
ADC_MAX_COUNTS = 65535   # ADC della Nucleo: 16 bit (ADC_RESOLUTION_16B)
DEFAULT_FS     = 100000  # sample rate ADC [Sa/s], sovrascritto dall'header traccia
VREF           = 3.3

def voltage_to_counts(v, vref=VREF):
    return int(max(0, min(DAC_MAX_COUNTS, round(v / vref * DAC_MAX_COUNTS))))

class AntiSELDashboard(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Tema e Colori
        ctk.set_appearance_mode("Light")
        ctk.set_default_color_theme("blue")

        self.title("AntiSEL Dashboard")
        self.geometry("1100x780")
        self.minsize(900, 600)

        # Variabili di stato
        self.sock             = None
        self.connected        = False
        self.rx_queue         = queue.Queue()
        self.rx_thread        = None

        self.ping_loop_active = False
        self.wave_active      = False
        self.wave_thread      = None
        self.trace_active     = False
        self.trace_file       = None
        self.trace_fs         = DEFAULT_FS
        self.trace_sample_idx = 0
        self.log_csv          = None   # file CSV log 10 Hz (R-08)
        self.events_csv       = None   # file CSV eventi/override (spec §5.3)
        self._ith_after_id    = None   # debounce slider I_TH

        self.tx_count = 0
        self.rx_count = 0

        # Variabili Hardware
        self.r_shunt = tk.StringVar(value="1.0")
        self.ina_gain = tk.StringVar(value="20")
        self.wave_type = tk.StringVar(value="Sinusoidale")

        # Info run (spec §6.3: nomenclatura <DUT_id>_<LET>_<run_id>_<timestamp>)
        self.dut_id = tk.StringVar(value="AD8629-01")
        self.let_id = tk.StringVar(value="LET00")
        self.run_id = tk.StringVar(value="RUN01")

        self._build_ui()
        self.after(100, self._poll_rx_queue)

    # ================================================================ UI BUILD
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- SIDEBAR (Sinistra) ---
        self.sidebar_frame = ctk.CTkFrame(self, width=240, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(8, weight=1) # Spazio vuoto

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="AntiSEL\nControl", font=ctk.CTkFont(size=24, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        # Connessione
        self.lbl_status = ctk.CTkLabel(self.sidebar_frame, text="● DISCONNESSO", text_color="red", font=ctk.CTkFont(weight="bold"))
        self.lbl_status.grid(row=1, column=0, padx=20, pady=5)

        self.lbl_target = ctk.CTkLabel(self.sidebar_frame, text=f"{HOST}:{PORT}")
        self.lbl_target.grid(row=2, column=0, padx=20, pady=0)

        self.btn_conn = ctk.CTkButton(self.sidebar_frame, text="Connetti", command=self._toggle_connection)
        self.btn_conn.grid(row=3, column=0, padx=20, pady=15)

        # Metriche
        self.metrics_frame = ctk.CTkFrame(self.sidebar_frame)
        self.metrics_frame.grid(row=4, column=0, padx=15, pady=10, sticky="ew")

        self.metric_ping = self._add_metric(self.metrics_frame, 0, "RTT ping", "— ms")
        self.metric_tx   = self._add_metric(self.metrics_frame, 1, "TX", "0")
        self.metric_rx   = self._add_metric(self.metrics_frame, 2, "RX", "0")

        # Comandi Rete / Test
        self.lbl_test = ctk.CTkLabel(self.sidebar_frame, text="Test di Rete", font=ctk.CTkFont(weight="bold"))
        self.lbl_test.grid(row=5, column=0, padx=20, pady=(20, 5))

        self.btn_ping = ctk.CTkButton(self.sidebar_frame, text="PING", command=lambda: self._send_cmd("PING"))
        self.btn_ping.grid(row=6, column=0, padx=20, pady=5)

        self.btn_ping_loop = ctk.CTkButton(self.sidebar_frame, text="Ping Loop", command=self._toggle_ping_loop, fg_color="transparent", border_width=2, text_color=("gray10", "#DCE4EE"))
        self.btn_ping_loop.grid(row=7, column=0, padx=20, pady=5)

        self.btn_status = ctk.CTkButton(self.sidebar_frame, text="STATUS", command=lambda: self._send_cmd("STATUS"))
        self.btn_status.grid(row=8, column=0, padx=20, pady=5, sticky="n")

        # Comando Manuale
        self.entry_cmd = ctk.CTkEntry(self.sidebar_frame, placeholder_text="Comando libero...")
        self.entry_cmd.grid(row=9, column=0, padx=20, pady=(10, 5), sticky="ew")
        self.entry_cmd.bind("<Return>", lambda e: self._send_manual())
        self.btn_send = ctk.CTkButton(self.sidebar_frame, text="Invia", command=self._send_manual)
        self.btn_send.grid(row=10, column=0, padx=20, pady=(0, 20), sticky="ew")


        # --- AREA CENTRALE (Destra) ---
        self.main_area = ctk.CTkFrame(self, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.main_area.grid_rowconfigure(1, weight=1) # Log area espande
        self.main_area.grid_columnconfigure(0, weight=1)

        # Tabs in alto
        self.tabview = ctk.CTkTabview(self.main_area, height=380)
        self.tabview.grid(row=0, column=0, sticky="new")
        self.tabview.add("Gestione AntiSEL")
        self.tabview.add("Generatore d'Onda")

        self._build_antisel_tab(self.tabview.tab("Gestione AntiSEL"))
        self._build_wave_tab(self.tabview.tab("Generatore d'Onda"))

        # Log Area in basso
        self._build_log_area(self.main_area)


    def _add_metric(self, parent, row, label, value):
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(size=11, weight="bold")).grid(row=row, column=0, padx=10, pady=2, sticky="e")
        val_lbl = ctk.CTkLabel(parent, text=value, font=ctk.CTkFont(size=12))
        val_lbl.grid(row=row, column=1, padx=10, pady=2, sticky="w")
        return val_lbl


    # ---------------------------------------------------------------- Tab AntiSEL
    def _build_antisel_tab(self, parent):
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_columnconfigure(2, weight=1)

        # Azioni Globali DUT
        frame_actions = ctk.CTkFrame(parent, fg_color="transparent")
        frame_actions.grid(row=0, column=0, columnspan=3, pady=(10, 10), sticky="ew")

        ctk.CTkButton(frame_actions, text="DUT ON", fg_color="green", hover_color="darkgreen", command=lambda: self._send_cmd("DUT_ON")).pack(side="left", padx=10, expand=True)
        ctk.CTkButton(frame_actions, text="DUT OFF", fg_color="red", hover_color="darkred", command=lambda: self._send_cmd("DUT_OFF")).pack(side="left", padx=10, expand=True)
        ctk.CTkButton(frame_actions, text="RESET", fg_color="orange", hover_color="darkorange", text_color="black", command=lambda: self._send_cmd("RESET")).pack(side="left", padx=10, expand=True)

        # Info Run (spec §6.3)
        frame_run = ctk.CTkFrame(parent)
        frame_run.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 5), sticky="ew")
        ctk.CTkLabel(frame_run, text="Run:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(10, 0), pady=5)
        for lbl, var, w in (("DUT id", self.dut_id, 110), ("LET", self.let_id, 70), ("Run id", self.run_id, 90)):
            ctk.CTkLabel(frame_run, text=f"{lbl}:").pack(side="left", padx=(12, 3))
            ctk.CTkEntry(frame_run, textvariable=var, width=w).pack(side="left")
        ctk.CTkLabel(frame_run, text="(usati nei nomi dei file CSV)", font=ctk.CTkFont(size=10)).pack(side="left", padx=12)

        # Hardware Setup
        frame_hw = ctk.CTkFrame(parent)
        frame_hw.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")

        ctk.CTkLabel(frame_hw, text="Hardware", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=2, pady=5)

        ctk.CTkLabel(frame_hw, text="R_SHUNT (Ω):").grid(row=1, column=0, padx=10, pady=5, sticky="e")
        self.entry_rshunt = ctk.CTkEntry(frame_hw, textvariable=self.r_shunt, width=80)
        self.entry_rshunt.grid(row=1, column=1, padx=10, pady=5)

        ctk.CTkLabel(frame_hw, text="Gain INA301:").grid(row=2, column=0, padx=10, pady=5, sticky="e")
        self.entry_gain = ctk.CTkEntry(frame_hw, textvariable=self.ina_gain, width=80)
        self.entry_gain.grid(row=2, column=1, padx=10, pady=5)

        # Metrica DAC in lettura
        self.metric_dac  = self._add_metric(frame_hw, 3, "DAC read", "— counts")
        self.metric_dacv = self._add_metric(frame_hw, 4, "Volt read", "— V")

        # Metriche di Stato AntiSEL
        self.metric_state   = self._add_metric(frame_hw, 5, "Stato MCU", "—")
        self.metric_retries = self._add_metric(frame_hw, 6, "Tentativi SEL", "0/3")
        self.metric_sel     = self._add_metric(frame_hw, 7, "Eventi SEL", "0")
        self.metric_hce     = self._add_metric(frame_hw, 8, "Eventi HCE", "0")

        # Configurazione Parametri
        frame_cfg = ctk.CTkFrame(parent)
        frame_cfg.grid(row=2, column=1, columnspan=2, padx=10, pady=5, sticky="nsew")
        frame_cfg.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frame_cfg, text="Soglie e Tempistiche", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=4, pady=5)

        # I_TH
        ctk.CTkLabel(frame_cfg, text="I_TH (mA):").grid(row=1, column=0, padx=10, pady=10, sticky="e")
        self.ith_slider = ctk.CTkSlider(frame_cfg, from_=1.0, to=50.0, command=self._on_ith_change)
        self.ith_slider.set(10.0)
        self.ith_slider.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

        self.lbl_ith_calc = ctk.CTkLabel(frame_cfg, text="10.0 mA\n(DAC: 0)", font=ctk.CTkFont(size=11))
        self.lbl_ith_calc.grid(row=1, column=2, padx=10, pady=10)

        # T_HOLD
        ctk.CTkLabel(frame_cfg, text="T_HOLD (ms):").grid(row=2, column=0, padx=10, pady=10, sticky="e")
        self.lbl_thold_val = ctk.CTkLabel(frame_cfg, text="1.0", width=30)
        self.lbl_thold_val.grid(row=2, column=2, padx=5, pady=10)
        self.thold_slider = ctk.CTkSlider(frame_cfg, from_=1.0, to=10.0, command=lambda v: self.lbl_thold_val.configure(text=f"{v:.1f}"))
        self.thold_slider.set(1.0)
        self.thold_slider.grid(row=2, column=1, padx=10, pady=10, sticky="ew")
        self.btn_thold = ctk.CTkButton(frame_cfg, text="Set", width=50, command=lambda: self._send_cmd(f"THOLD_SET {self.thold_slider.get():.1f}"))
        self.btn_thold.grid(row=2, column=3, padx=10, pady=10)

        # T_ON
        ctk.CTkLabel(frame_cfg, text="T_ON (ms):").grid(row=3, column=0, padx=10, pady=10, sticky="e")
        self.lbl_ton_val = ctk.CTkLabel(frame_cfg, text="1.0", width=30)
        self.lbl_ton_val.grid(row=3, column=2, padx=5, pady=10)
        self.ton_slider = ctk.CTkSlider(frame_cfg, from_=1.0, to=10.0, command=lambda v: self.lbl_ton_val.configure(text=f"{v:.1f}"))
        self.ton_slider.set(1.0)
        self.ton_slider.grid(row=3, column=1, padx=10, pady=10, sticky="ew")
        self.btn_ton = ctk.CTkButton(frame_cfg, text="Set", width=50, command=lambda: self._send_cmd(f"TON_SET {self.ton_slider.get():.1f}"))
        self.btn_ton.grid(row=3, column=3, padx=10, pady=10)

        # Soglie precaricate (spec §8.2): benchmark + 2 alternative
        ctk.CTkLabel(frame_cfg, text="Preset I_TH:").grid(row=4, column=0, padx=10, pady=10, sticky="e")
        frame_th = ctk.CTkFrame(frame_cfg, fg_color="transparent")
        frame_th.grid(row=4, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        for n in (1, 2, 3):
            ctk.CTkButton(frame_th, text=f"Carica {n}", width=68,
                          command=lambda n=n: self._th_load(n)).pack(side="left", padx=3)
            ctk.CTkButton(frame_th, text=f"Usa {n}", width=55, fg_color="gray40", hover_color="gray25",
                          command=lambda n=n: self._send_cmd(f"TH_SELECT {n}")).pack(side="left", padx=(0, 8))

    def _on_ith_change(self, val):
        try:
            r_shunt = float(self.r_shunt.get())
            gain = float(self.ina_gain.get())
            i_th_mA = float(val)
            v_limit = (i_th_mA / 1000.0) * r_shunt * gain
            counts = voltage_to_counts(v_limit)
            self.lbl_ith_calc.configure(text=f"{i_th_mA:.1f} mA\n(DAC: {counts})")
            # Debounce: invia il DAC_SET solo 200 ms dopo l'ultimo movimento
            # dello slider, per non inondare il link TCP
            if self._ith_after_id is not None:
                self.after_cancel(self._ith_after_id)
            self._ith_after_id = self.after(200, lambda c=counts: self._send_cmd(f"DAC_SET {c}"))
        except ValueError:
            pass

    def _th_load(self, n):
        """Carica il valore corrente dello slider I_TH nella preset n della
        scheda (spec §8.2)."""
        try:
            r_shunt = float(self.r_shunt.get())
            gain = float(self.ina_gain.get())
            i_th_mA = float(self.ith_slider.get())
            counts = voltage_to_counts((i_th_mA / 1000.0) * r_shunt * gain)
            self._send_cmd(f"TH_LOAD {n} {counts}")
            self._log(f"Preset {n} <- {i_th_mA:.1f} mA ({counts} counts)", "info")
        except ValueError:
            pass

    # ---------------------------------------------------------------- Tab Generatore
    def _build_wave_tab(self, parent):
        parent.grid_columnconfigure((0,1), weight=1)

        # Tipo
        frm_tipo = ctk.CTkFrame(parent)
        frm_tipo.grid(row=0, column=0, columnspan=2, padx=10, pady=10, sticky="ew")
        ctk.CTkLabel(frm_tipo, text="Tipo Onda:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=10, pady=10)
        self.seg_wave = ctk.CTkSegmentedButton(frm_tipo, values=["Sinusoidale", "Quadra", "Triangolare"], variable=self.wave_type)
        self.seg_wave.pack(side="left", padx=10, pady=10, expand=True)

        # Parametri
        frm_par = ctk.CTkFrame(parent)
        frm_par.grid(row=1, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        frm_par.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frm_par, text="Freq (Hz):").grid(row=0, column=0, padx=10, pady=10, sticky="e")
        self.wave_freq = ctk.CTkSlider(frm_par, from_=0.1, to=10.0, command=self._update_wave_lbls)
        self.wave_freq.set(1.0)
        self.wave_freq.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        self.lbl_wave_f = ctk.CTkLabel(frm_par, text="1.0 Hz")
        self.lbl_wave_f.grid(row=0, column=2, padx=10, pady=10)

        ctk.CTkLabel(frm_par, text="V max:").grid(row=1, column=0, padx=10, pady=10, sticky="e")
        self.wave_vmax = ctk.CTkSlider(frm_par, from_=0.1, to=VREF, command=self._update_wave_lbls)
        self.wave_vmax.set(VREF)
        self.wave_vmax.grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        self.lbl_wave_v = ctk.CTkLabel(frm_par, text=f"{VREF:.2f} V")
        self.lbl_wave_v.grid(row=1, column=2, padx=10, pady=10)

        # Avvio
        self.btn_wave_start = ctk.CTkButton(parent, text="▶ Avvia Generatore", fg_color="green", hover_color="darkgreen", command=self._toggle_wave)
        self.btn_wave_start.grid(row=2, column=0, columnspan=2, pady=20)

    def _update_wave_lbls(self, _):
        self.lbl_wave_f.configure(text=f"{self.wave_freq.get():.1f} Hz")
        self.lbl_wave_v.configure(text=f"{self.wave_vmax.get():.2f} V")

    # ---------------------------------------------------------------- Logs
    def _build_log_area(self, parent):
        log_container = ctk.CTkFrame(parent)
        log_container.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        log_container.grid_columnconfigure((0,1), weight=1)
        log_container.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(log_container, text="Log Comunicazione TCP", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, pady=5)
        ctk.CTkLabel(log_container, text="Log 10Hz & Event Traces", font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, pady=5)

        self.log = ctk.CTkTextbox(log_container, font=ctk.CTkFont(family="Courier", size=12), state="disabled")
        self.log.grid(row=1, column=0, padx=10, pady=(0,10), sticky="nsew")

        self.slow_log = ctk.CTkTextbox(log_container, font=ctk.CTkFont(family="Courier", size=12), state="disabled", fg_color="#F0F0F0")
        self.slow_log.grid(row=1, column=1, padx=10, pady=(0,10), sticky="nsew")

        # Configurazione tags colore (light theme friendly)
        self.log.tag_config("tx", foreground="#0052cc")
        self.log.tag_config("rx", foreground="#008000")
        self.log.tag_config("info", foreground="#b35900")
        self.log.tag_config("err", foreground="#cc0000")

        self.slow_log.tag_config("trace", foreground="#800080")


    # ============================================================ CONNESSIONE
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
            self.after(0, self._on_connected)
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()
        except Exception as e:
            self.rx_queue.put(("err", f"Connessione fallita: {e}"))

    def _disconnect(self):
        self.wave_active      = False
        self.ping_loop_active = False
        self.connected        = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self._on_disconnected()

    def _on_connected(self):
        self.lbl_status.configure(text="● CONNESSO", text_color="green")
        self.btn_conn.configure(text="Disconnetti", fg_color="red", hover_color="darkred")
        self._log("Connesso.", "info")
        # Apri i file CSV del run con nomenclatura §6.3:
        # <DUT_id>_<LET>_<run_id>_<timestamp>_{log10hz|events}.csv
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            prefix = f"{self._run_prefix()}_{ts}"
            self.log_csv = open(f"{prefix}_log10hz.csv", "w")
            self.log_csv.write("PC_Time,Tick_ms,ADC_raw,Current_mA,Fresh,State,Retry,SEL,HCE\n")
            self.events_csv = open(f"{prefix}_events.csv", "w")
            self.events_csv.write("PC_Time,Event,Detail\n")
            self._log(f"File di run: {prefix}_*.csv", "info")
            self._log_event("CONNECT", f"{HOST}:{PORT}")
        except Exception as e:
            self.log_csv = None
            self.events_csv = None
            self._log(f"Errore apertura file di run: {e}", "err")

    def _on_disconnected(self):
        self._log_event("DISCONNECT")
        if self.log_csv:
            try:
                self.log_csv.close()
            except Exception:
                pass
            self.log_csv = None
        if self.events_csv:
            try:
                self.events_csv.close()
            except Exception:
                pass
            self.events_csv = None
        self.lbl_status.configure(text="● DISCONNESSO", text_color="red")
        self.btn_conn.configure(text="Connetti", fg_color=["#3B8ED0", "#1F6AA5"], hover_color=["#36719F", "#144870"])
        self.btn_wave_start.configure(text="▶ Avvia Generatore", fg_color="green", hover_color="darkgreen")
        self.btn_ping_loop.configure(fg_color="transparent")
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
                self.after(0, self._on_disconnected)

    def _send_cmd(self, cmd):
        if not self.connected or not self.sock:
            return
        try:
            self.sock.sendall((cmd + "\r\n").encode())
            self.tx_count += 1
            self.after(0, lambda: self.metric_tx.configure(text=str(self.tx_count)))
            if not cmd.startswith("DAC_SET"):
                self._log(f"-> {cmd}", "tx")
            # Log eventi/override con timestamp (spec §5.3)
            if cmd.startswith(("DUT_ON", "DUT_OFF", "RESET", "TH_SELECT",
                               "TH_LOAD", "THOLD_SET", "TON_SET")):
                parts = cmd.split(None, 1)
                self._log_event(parts[0], parts[1] if len(parts) > 1 else "")
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
                    if msg.startswith("LOG_10HZ"):
                        # Es: LOG_10HZ TICK=1234 I=567 FRESH=1 STATE=0 RETRY=1 SEL=0 HCE=0
                        fields = self._parse_kv(msg)
                        try:
                            st = int(fields.get("STATE", -1))
                            if st >= 0:
                                st_str = ["IDLE", "T_HOLD", "T_ON", "PERMANENT_OFF"][st] if st < 4 else str(st)
                                self.metric_state.configure(text=st_str, text_color="red" if st == 3 else "black")
                            if "RETRY" in fields:
                                ret = int(fields["RETRY"])
                                self.metric_retries.configure(text=f"{ret}/3", text_color="red" if ret >= 3 else "black")
                            if "SEL" in fields:
                                self.metric_sel.configure(text=fields["SEL"])
                            if "HCE" in fields:
                                self.metric_hce.configure(text=fields["HCE"])
                            # Scrittura CSV (R-08): timestamp PC + dato convertito in mA
                            if self.log_csv and "I" in fields:
                                adc_raw = int(fields["I"])
                                i_mA = self._counts_to_mA(adc_raw)
                                pc_ts = time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
                                self.log_csv.write(
                                    f"{pc_ts},{fields.get('TICK', '')},{adc_raw},{i_mA},"
                                    f"{fields.get('FRESH', '')},{fields.get('STATE', '')},"
                                    f"{fields.get('RETRY', '')},{fields.get('SEL', '')},{fields.get('HCE', '')}\n"
                                )
                        except Exception:
                            pass

                        self._log_slow(msg)
                        continue
                    elif msg.startswith("TRACE_START"):
                        # Es: TRACE_START SEL FS=100000 N=2108 THOLD_MS=1.0 TON_MS=1.0 DAC=2048 TICK=1234
                        self.trace_active = True
                        self.trace_sample_idx = 0
                        fields = self._parse_kv(msg)
                        try:
                            self.trace_fs = int(fields.get("FS", DEFAULT_FS))
                        except Exception:
                            self.trace_fs = DEFAULT_FS
                        self._log_slow(f"--- {msg} ---", "trace")
                        try:
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            toks = msg.split()
                            ev = toks[1] if len(toks) > 1 else "EVT"
                            fname = f"{self._run_prefix()}_{ts}_trace_{ev}.csv"
                            self.trace_file = open(fname, "w")
                            # Metadati richiesti da spec §6.1 come commenti
                            self.trace_file.write(f"# {msg}\n")
                            self.trace_file.write(f"# PC_Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
                            self.trace_file.write(f"# R_SHUNT_ohm: {self.r_shunt.get()}  INA_GAIN: {self.ina_gain.get()}\n")
                            self.trace_file.write("Time_us,ADC_raw,Current_mA\n")
                            self._log_event("TRACE", f"{ev} {fname}")
                        except Exception as e:
                            self._log(f"Errore file traccia: {e}", "err")
                        continue
                    elif msg.startswith("TRACE_END"):
                        self.trace_active = False
                        self._log_slow(f"--- {msg} ({self.trace_sample_idx} campioni) ---", "trace")
                        if self.trace_file:
                            self.trace_file.close()
                            self.trace_file = None
                        continue
                    elif self.trace_active:
                        # Righe traccia dal firmware: "indice,valore_raw"
                        if self.trace_file:
                            try:
                                parts = msg.split(",")
                                if len(parts) == 2:
                                    idx = int(parts[0])
                                    adc_raw = int(parts[1])
                                    time_us = idx * 1e6 / self.trace_fs
                                    i_mA = self._counts_to_mA(adc_raw)
                                    self.trace_file.write(f"{time_us:.1f},{adc_raw},{i_mA}\n")
                                    self.trace_sample_idx += 1
                                else:
                                    self.trace_file.write(f"# {msg}\n")
                            except Exception:
                                self.trace_file.write(f"# {msg}\n")
                        continue

                    self.rx_count += 1
                    self.metric_rx.configure(text=str(self.rx_count))

                    if msg.startswith("DAC="):
                        try:
                            counts = int(msg.split("=")[1])
                            v = counts / DAC_MAX_COUNTS * VREF
                            self.metric_dac.configure(text=str(counts))
                            self.metric_dacv.configure(text=f"{v:.2f} V")
                        except Exception:
                            pass

                    if not (self.wave_active and msg.startswith("DAC_SET")):
                        self._log(f"<- {msg}", "rx")
                elif kind == "err":
                    self._log(msg, "err")
                elif kind == "rtt":
                    self.metric_ping.configure(text=f"{msg:.1f} ms")
        except queue.Empty:
            pass
        self.after(100, self._poll_rx_queue)

    # ============================================================ HELPERS
    @staticmethod
    def _parse_kv(msg):
        """Estrae i campi KEY=VALUE da una riga di protocollo."""
        out = {}
        for tok in msg.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                out[k] = v
        return out

    def _counts_to_mA(self, adc_raw):
        """Converte un campione ADC 16-bit in mA (stringa) usando R_SHUNT e
        gain INA301 correnti; '' se i parametri non sono validi."""
        try:
            r_shunt = float(self.r_shunt.get())
            gain = float(self.ina_gain.get())
            v_adc = (adc_raw / ADC_MAX_COUNTS) * VREF
            return f"{(v_adc / (r_shunt * gain)) * 1000.0:.3f}"
        except (ValueError, ZeroDivisionError):
            return ""

    def _run_prefix(self):
        """Prefisso file secondo spec §6.3: <DUT_id>_<LET>_<run_id>."""
        def clean(s):
            s = s.strip().replace(" ", "-")
            s = "".join(ch for ch in s if ch.isalnum() or ch in "-_.")
            return s or "NA"
        return f"{clean(self.dut_id.get())}_{clean(self.let_id.get())}_{clean(self.run_id.get())}"

    def _log_event(self, event, detail=""):
        """Scrive un evento (override manuale, cambio parametri, tracce)
        sul CSV eventi con timestamp PC (spec §5.3)."""
        if self.events_csv:
            try:
                pc_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.events_csv.write(f"{pc_ts},{event},{detail}\n")
                self.events_csv.flush()
            except Exception:
                pass

    # ============================================================ PING LOOP
    def _toggle_ping_loop(self):
        if self.ping_loop_active:
            self.ping_loop_active = False
            self.btn_ping_loop.configure(fg_color="transparent")
        else:
            self.ping_loop_active = True
            self.btn_ping_loop.configure(fg_color="orange")
            threading.Thread(target=self._ping_loop_thread, daemon=True).start()

    def _ping_loop_thread(self):
        while self.ping_loop_active and self.connected:
            try:
                t0 = time.time()
                self.sock.sendall(b"PING\r\n")
                self.tx_count += 1
                self.after(0, lambda: self.metric_tx.configure(text=str(self.tx_count)))
                self._log("-> PING", "tx")

                kind, msg = self.rx_queue.get(timeout=3.0)
                rtt = (time.time() - t0) * 1000
                self.rx_count += 1
                self.after(0, lambda r=msg, ms=rtt: (
                    self.metric_rx.configure(text=str(self.rx_count)),
                    self.metric_ping.configure(text=f"{ms:.1f} ms"),
                    self._log(f"<- {r}  [{ms:.1f} ms]", "rx")
                ))
            except Exception as e:
                self.rx_queue.put(("err", f"Ping loop error: {e}"))
                self.ping_loop_active = False
                break
            time.sleep(1.0)

    # ============================================================ GENERATORE
    def _toggle_wave(self):
        if self.wave_active:
            self.wave_active = False
            self.btn_wave_start.configure(text="▶ Avvia Generatore", fg_color="green", hover_color="darkgreen")
        else:
            if not self.connected:
                self._log("Non connesso.", "err")
                return
            self.wave_active = True
            self.btn_wave_start.configure(text="■ Stop Generatore", fg_color="red", hover_color="darkred")
            self.wave_thread = threading.Thread(target=self._wave_loop, daemon=True)
            self.wave_thread.start()

    def _wave_loop(self):
        phase = 0.0
        while self.wave_active and self.connected:
            freq    = self.wave_freq.get()
            vmax    = self.wave_vmax.get()
            wtype   = self.wave_type.get()
            samples = 32
            dt = 1.0 / (freq * samples)

            if wtype == "Sinusoidale":
                norm = (math.sin(phase) + 1) / 2
            elif wtype == "Quadra":
                norm = 1.0 if math.sin(phase) >= 0 else 0.0
            else:
                t = phase / (2 * math.pi)
                norm = 2 * abs(t - math.floor(t + 0.5))

            v = vmax * norm
            v = max(0.0, min(VREF, v))
            counts = voltage_to_counts(v)

            self._send_cmd(f"DAC_SET {counts}")

            phase += 2 * math.pi / samples
            if phase >= 2 * math.pi:
                phase -= 2 * math.pi

            time.sleep(dt)

        self._send_cmd("DAC_SET 0")


    # ================================================================== LOG
    def _log(self, msg, tag="info"):
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] {msg}\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_slow(self, msg, tag=None):
        ts = time.strftime("%H:%M:%S")
        self.slow_log.configure(state="normal")
        if tag:
            self.slow_log.insert("end", f"[{ts}] {msg}\n", tag)
        else:
            self.slow_log.insert("end", f"[{ts}] {msg}\n")
        self.slow_log.see("end")
        self.slow_log.configure(state="disabled")


if __name__ == "__main__":
    app = AntiSELDashboard()
    app.mainloop()
