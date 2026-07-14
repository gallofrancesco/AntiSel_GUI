"""
AntiSEL Dashboard v4.5 — Ethernet TCP (CustomTkinter UI)
Comunicazione con NUCLEO-H755ZI-Q @ 192.168.1.100:7755

v4.5:
- Layout a 3 colonne: [rete] | [controlli] | [grafici sempre visibili]
- I_TH numerico e preciso (entry + step ±0.1/±1 mA), niente slider
- T_HOLD / T_ON come campi numerici
Storia precedente (firmware latched, discriminazione ADC, controlli latch,
grafici corrente/tensione + traccia) invariata.
"""

import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
import socket
import threading
import time
import queue
from collections import deque
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

HOST    = "192.168.1.100"
PORT    = 7755
TIMEOUT = 3.0

DAC_MAX_COUNTS = 4095    # DAC della Nucleo: 12 bit
ADC_MAX_COUNTS = 65535   # ADC della Nucleo: 16 bit (ADC_RESOLUTION_16B)
DEFAULT_FS     = 100000  # sample rate ADC [Sa/s], sovrascritto dall'header traccia
VREF           = 3.3

# Nomi stati allineati al firmware Fase 2 (macchina a 11 stati)
STATE_NAMES = ["INIT", "IDLE", "ALARM", "HOLD_RUN", "HCE_SAVE", "CUTOFF",
               "TON_RUN", "RECOVERY", "VERIFY", "MANUAL_OFF", "FAULT"]
SEL_RETRY_MAX = 3

I_TH_MIN, I_TH_MAX = 1.0, 50.0   # range soglia (R-02)


def voltage_to_counts(v, vref=VREF):
    return int(max(0, min(DAC_MAX_COUNTS, round(v / vref * DAC_MAX_COUNTS))))


class AntiSELDashboard(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("Light")
        ctk.set_default_color_theme("blue")

        self.title("AntiSEL Dashboard")
        self.geometry("1560x860")
        self.minsize(1280, 720)

        # Stato
        self.sock             = None
        self.connected        = False
        self.rx_queue         = queue.Queue()
        self.rx_thread        = None
        self.ping_loop_active = False
        self.trace_active     = False
        self.trace_file       = None
        self.trace_fs         = DEFAULT_FS
        self.trace_sample_idx = 0
        self.log_csv          = None
        self.events_csv       = None
        self.ping_sent_time   = None
        self._last_rtt        = None   # RTT misurato all'arrivo del PONG [ms]
        self.permanent_off    = False
        self.tx_count = 0
        self.rx_count = 0
        self.cur_dac  = 2048   # ultimo valore DAC (soglia attiva)

        # Variabili
        self.r_shunt  = tk.StringVar(value="1.0")
        self.ina_gain = tk.StringVar(value="20")
        self.ith_val  = tk.StringVar(value="10.0")   # I_TH [mA] preciso
        self.thold_val = tk.StringVar(value="5.0")   # T_HOLD [ms]
        self.ton_val   = tk.StringVar(value="2.0")   # T_ON [ms]
        self.dut_id = tk.StringVar(value="AD8629-01")
        self.let_id = tk.StringVar(value="LET00")
        self.run_id = tk.StringVar(value="RUN01")
        self.retry_max = tk.StringVar(value="3")
        self.t_clear   = tk.StringVar(value="30")

        # Buffer grafici
        self.slow_t   = deque(maxlen=600)
        self.slow_i   = deque(maxlen=600)
        self.slow_thr = deque(maxlen=600)
        self.slow_t0  = None
        self._trace_acc = []
        self.trace_x  = []
        self.trace_y  = []
        self.trace_lbl = ""
        self._plot_dirty = False
        self.plot_paused = False

        self._build_ui()
        self.after(40, self._poll_rx_queue)
        self.after(400, self._refresh_plots)

    # ================================================================ UI
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=215)   # rete
        self.grid_columnconfigure(1, weight=2, minsize=500)   # controlli (si allarga)
        self.grid_columnconfigure(2, weight=3, minsize=420)   # grafici (espande)
        self.grid_rowconfigure(0, weight=3)
        self.grid_rowconfigure(1, weight=1)                   # log

        self.col_left  = ctk.CTkFrame(self, width=230, corner_radius=0)
        self.col_left.grid(row=0, column=0, sticky="nsew")
        self.col_mid   = ctk.CTkScrollableFrame(self, label_text="Controlli AntiSEL")
        self.col_mid.grid(row=0, column=1, sticky="nsew", padx=(6, 3), pady=6)
        self.col_right = ctk.CTkFrame(self, fg_color="transparent")
        self.col_right.grid(row=0, column=2, sticky="nsew", padx=(3, 6), pady=6)

        self._build_left(self.col_left)
        self._build_center(self.col_mid)
        self._build_charts(self.col_right)

        self.log_frame = ctk.CTkFrame(self)
        self.log_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=6, pady=(0, 6))
        self._build_log_area(self.log_frame)

    def _add_metric(self, parent, row, label, value):
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(size=11, weight="bold")).grid(row=row, column=0, padx=10, pady=2, sticky="e")
        val_lbl = ctk.CTkLabel(parent, text=value, font=ctk.CTkFont(size=12))
        val_lbl.grid(row=row, column=1, padx=10, pady=2, sticky="w")
        return val_lbl

    # ---------------------------------------------------------------- Colonna SX
    def _build_left(self, p):
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(7, weight=1)

        ctk.CTkLabel(p, text="AntiSEL\nControl", font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, padx=20, pady=(18, 8))

        self.lbl_status = ctk.CTkLabel(p, text="● DISCONNESSO", text_color="red", font=ctk.CTkFont(weight="bold"))
        self.lbl_status.grid(row=1, column=0, padx=20, pady=4)
        self.lbl_target = ctk.CTkLabel(p, text=f"{HOST}:{PORT}")
        self.lbl_target.grid(row=2, column=0, padx=20)
        self.btn_conn = ctk.CTkButton(p, text="Connetti", command=self._toggle_connection)
        self.btn_conn.grid(row=3, column=0, padx=20, pady=12)

        mf = ctk.CTkFrame(p)
        mf.grid(row=4, column=0, padx=15, pady=8, sticky="ew")
        self.metric_ping = self._add_metric(mf, 0, "RTT ping", "— ms")
        self.metric_tx   = self._add_metric(mf, 1, "TX", "0")
        self.metric_rx   = self._add_metric(mf, 2, "RX", "0")

        ctk.CTkLabel(p, text="Test di Rete", font=ctk.CTkFont(weight="bold")).grid(row=5, column=0, padx=20, pady=(14, 4))
        tf = ctk.CTkFrame(p, fg_color="transparent")
        tf.grid(row=6, column=0, padx=15, pady=2, sticky="ew")
        tf.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(tf, text="PING", command=lambda: self._send_cmd("PING")).grid(row=0, column=0, padx=3, pady=3, sticky="ew")
        ctk.CTkButton(tf, text="STATUS", command=lambda: self._send_cmd("STATUS")).grid(row=0, column=1, padx=3, pady=3, sticky="ew")
        self.btn_ping_loop = ctk.CTkButton(tf, text="Ping Loop", command=self._toggle_ping_loop, fg_color="transparent", border_width=2, text_color=("gray10", "#DCE4EE"))
        self.btn_ping_loop.grid(row=1, column=0, columnspan=2, padx=3, pady=3, sticky="ew")

        # spacer row 7

        cf = ctk.CTkFrame(p, fg_color="transparent")
        cf.grid(row=8, column=0, padx=15, pady=(6, 16), sticky="ew")
        cf.grid_columnconfigure(0, weight=1)
        self.entry_cmd = ctk.CTkEntry(cf, placeholder_text="Comando libero...")
        self.entry_cmd.grid(row=0, column=0, pady=(0, 5), sticky="ew")
        self.entry_cmd.bind("<Return>", lambda e: self._send_manual())
        ctk.CTkButton(cf, text="Invia", command=self._send_manual).grid(row=1, column=0, sticky="ew")

    # ---------------------------------------------------------------- Colonna CENTRO
    def _build_center(self, p):
        p.grid_columnconfigure(0, weight=1)
        r = 0

        # --- Azioni DUT ---
        sec = self._section(p, r, "Azioni DUT"); r += 1
        act = ctk.CTkFrame(sec, fg_color="transparent"); act.pack(fill="x")
        self.btn_dut_on = ctk.CTkButton(act, text="DUT ON", fg_color="green", hover_color="darkgreen", command=lambda: self._send_cmd("DUT_ON"))
        self.btn_dut_on.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_dut_off = ctk.CTkButton(act, text="DUT OFF", fg_color="red", hover_color="darkred", command=lambda: self._send_cmd("DUT_OFF"))
        self.btn_dut_off.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_reset = ctk.CTkButton(act, text="RESET", fg_color="orange", hover_color="darkorange", text_color="black", command=lambda: self._send_cmd("RESET"))
        self.btn_reset.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_ack = ctk.CTkButton(act, text="ACK FAULT", fg_color="#8a4500", hover_color="#6a3500", command=lambda: self._send_cmd("ACK FAULT"))
        self.btn_ack.pack(side="left", padx=4, expand=True, fill="x")
        self.lbl_perm_warn = ctk.CTkLabel(sec, text="", text_color="#cc0000", justify="left", font=ctk.CTkFont(size=12, weight="bold"))

        # --- Latch INA301 ---
        sec = self._section(p, r, "Latch INA301 (policy riarmo)"); r += 1
        lr = ctk.CTkFrame(sec, fg_color="transparent"); lr.pack(fill="x")
        self.btn_ina_rst = ctk.CTkButton(lr, text="Reset allarme", width=110, fg_color="#b35900", hover_color="#8a4500", command=lambda: self._send_cmd("INA_RST"))
        self.btn_ina_rst.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(lr, text="N:").pack(side="left")
        ctk.CTkEntry(lr, textvariable=self.retry_max, width=42).pack(side="left", padx=(2, 4))
        ctk.CTkButton(lr, text="Set", width=40, command=lambda: self._send_cmd(f"SET RETRY_MAX {self.retry_max.get()}")).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(lr, text="T_CLEAR:").pack(side="left")
        ctk.CTkEntry(lr, textvariable=self.t_clear, width=52).pack(side="left", padx=(2, 4))
        ctk.CTkButton(lr, text="Set", width=40, command=lambda: self._send_cmd(f"SET TCLEAR_MS {self.t_clear.get()}")).pack(side="left")

        # --- Soglia I_TH (precisa) ---
        sec = self._section(p, r, "Soglia di corrente I_TH"); r += 1
        ithr = ctk.CTkFrame(sec, fg_color="transparent"); ithr.pack(fill="x")
        ctk.CTkLabel(ithr, text="I_TH (mA):").pack(side="left", padx=(0, 4))
        ctk.CTkButton(ithr, text="−1", width=34, command=lambda: self._ith_step(-1.0)).pack(side="left", padx=1)
        ctk.CTkButton(ithr, text="−0.1", width=40, command=lambda: self._ith_step(-0.1)).pack(side="left", padx=1)
        e = ctk.CTkEntry(ithr, textvariable=self.ith_val, width=70, justify="center")
        e.pack(side="left", padx=3); e.bind("<Return>", self._apply_ith)
        ctk.CTkButton(ithr, text="+0.1", width=40, command=lambda: self._ith_step(0.1)).pack(side="left", padx=1)
        ctk.CTkButton(ithr, text="+1", width=34, command=lambda: self._ith_step(1.0)).pack(side="left", padx=1)
        ctk.CTkButton(ithr, text="Set", width=44, command=self._apply_ith).pack(side="left", padx=(6, 0))
        self.lbl_ith_calc = ctk.CTkLabel(sec, text="DAC: — ( — V)", font=ctk.CTkFont(size=11))
        self.lbl_ith_calc.pack(anchor="w", pady=(4, 0))
        # Preset (§8.2) — opzionali in questa fase
        pr = ctk.CTkFrame(sec, fg_color="transparent"); pr.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(pr, text="Preset:", font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        for n in (1, 2, 3):
            ctk.CTkButton(pr, text=f"Carica {n}", width=64, command=lambda n=n: self._th_load(n)).pack(side="left", padx=2)
            ctk.CTkButton(pr, text=f"Usa {n}", width=48, fg_color="gray40", hover_color="gray25", command=lambda n=n: self._send_cmd(f"TH_SELECT {n}")).pack(side="left", padx=(0, 6))

        # --- Tempistiche ---
        sec = self._section(p, r, "Tempistiche"); r += 1
        tr = ctk.CTkFrame(sec, fg_color="transparent"); tr.pack(fill="x")
        ctk.CTkLabel(tr, text="T_HOLD (ms):").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(tr, textvariable=self.thold_val, width=60).pack(side="left")
        ctk.CTkButton(tr, text="Set", width=44, command=self._set_thold).pack(side="left", padx=(4, 16))
        ctk.CTkLabel(tr, text="T_ON (ms):").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(tr, textvariable=self.ton_val, width=60).pack(side="left")
        ctk.CTkButton(tr, text="Set", width=44, command=self._set_ton).pack(side="left", padx=(4, 0))

        # --- Hardware ---
        sec = self._section(p, r, "Hardware"); r += 1
        hr = ctk.CTkFrame(sec, fg_color="transparent"); hr.pack(fill="x")
        ctk.CTkLabel(hr, text="R_SHUNT (Ω):").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(hr, textvariable=self.r_shunt, width=70).pack(side="left", padx=(0, 16))
        ctk.CTkLabel(hr, text="Gain INA301:").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(hr, textvariable=self.ina_gain, width=70).pack(side="left")

        # --- Run ---
        sec = self._section(p, r, "Run (nomi file CSV)"); r += 1
        rr = ctk.CTkFrame(sec, fg_color="transparent"); rr.pack(fill="x")
        for lbl, var, w in (("DUT id", self.dut_id, 110), ("LET", self.let_id, 70), ("Run id", self.run_id, 90)):
            ctk.CTkLabel(rr, text=f"{lbl}:").pack(side="left", padx=(0, 3))
            ctk.CTkEntry(rr, textvariable=var, width=w).pack(side="left", padx=(0, 8))

        # --- Stato MCU ---
        sec = self._section(p, r, "Stato"); r += 1
        st = ctk.CTkFrame(sec, fg_color="transparent"); st.pack(fill="x")
        st.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.metric_state   = self._add_metric(st, 0, "Stato MCU", "—")
        self.metric_retries = self._add_metric(st, 1, "Tentativi", f"0/{SEL_RETRY_MAX}")
        self.metric_sel     = self._add_metric(st, 2, "SEL", "0")
        self.metric_hce     = self._add_metric(st, 3, "HCE", "0")
        st2 = ctk.CTkFrame(sec, fg_color="transparent"); st2.pack(fill="x")
        self.metric_dac  = self._add_metric(st2, 0, "DAC read", "— counts")
        self.metric_dacv = self._add_metric(st2, 1, "Volt read", "— V")

        self._apply_ith()  # inizializza lbl_ith_calc + cur_dac

    def _section(self, parent, row, title):
        f = ctk.CTkFrame(parent)
        f.grid(row=row, column=0, sticky="ew", padx=4, pady=4)
        ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=8, pady=(6, 2))
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.pack(fill="x", padx=8, pady=(0, 8))
        return body

    # ---------------------------------------------------------------- I_TH preciso
    def _apply_ith(self, *_):
        try:
            mA = float(self.ith_val.get())
        except ValueError:
            return
        mA = max(I_TH_MIN, min(I_TH_MAX, mA))
        self.ith_val.set(f"{mA:.1f}")
        try:
            r = float(self.r_shunt.get()); g = float(self.ina_gain.get())
        except ValueError:
            return
        counts = voltage_to_counts((mA / 1000.0) * r * g)
        self.cur_dac = counts
        self.lbl_ith_calc.configure(
            text=f"DAC≈{counts}  ({counts / DAC_MAX_COUNTS * VREF:.2f} V)  "
                 f"[soglia calcolata dal firmware]")
        # Protocollo v5: la soglia si invia in mA, il firmware calcola il DAC,
        # valida il range elettrico e diventa la fonte di verità.
        self._send_cmd(f"SET THRESHOLD_MA {mA:.1f}")

    def _ith_step(self, delta):
        try:
            mA = float(self.ith_val.get())
        except ValueError:
            mA = 10.0
        self.ith_val.set(f"{max(I_TH_MIN, min(I_TH_MAX, mA + delta)):.1f}")
        self._apply_ith()

    def _th_load(self, n):
        """Carica il valore I_TH corrente nella preset n (spec §8.2)."""
        try:
            r = float(self.r_shunt.get()); g = float(self.ina_gain.get())
            mA = float(self.ith_val.get())
            counts = voltage_to_counts((mA / 1000.0) * r * g)
            self._send_cmd(f"TH_LOAD {n} {counts}")
            self._log(f"Preset {n} <- {mA:.1f} mA ({counts} counts)", "info")
        except ValueError:
            pass

    # ---------------------------------------------------------------- Tempi (µs)
    def _set_thold(self):
        """T_HOLD: la GUI usa i ms, il firmware vuole i µs (protocollo v5)."""
        try:
            us = int(round(float(self.thold_val.get()) * 1000))
        except ValueError:
            return
        self._send_cmd(f"SET THOLD_US {us}")

    def _set_ton(self):
        try:
            us = int(round(float(self.ton_val.get()) * 1000))
        except ValueError:
            return
        self._send_cmd(f"SET TON_US {us}")

    def _send_config(self):
        """Invia l'intera config elettrica/parametrica al firmware alla
        connessione (protocollo v5 §7): il firmware diventa fonte di verità."""
        try:
            self._send_cmd(f"SET VREF_ADC {VREF:.3f}")
            self._send_cmd(f"SET VREF_DAC {VREF:.3f}")
            self._send_cmd(f"SET GAIN {int(float(self.ina_gain.get()))}")
            self._send_cmd(f"SET RSHUNT {float(self.r_shunt.get()):.3f}")
            self._send_cmd(f"SET THRESHOLD_MA {float(self.ith_val.get()):.1f}")
            self._set_thold()
            self._set_ton()
            self._send_cmd(f"SET RETRY_MAX {int(self.retry_max.get())}")
            self._send_cmd(f"SET TCLEAR_MS {int(self.t_clear.get())}")
            self._send_cmd("GET CONFIG")
        except (ValueError, AttributeError):
            self._log("Config non inviata: parametri non validi.", "err")

    # ---------------------------------------------------------------- Grafici
    def _build_charts(self, p):
        p.grid_rowconfigure(1, weight=1)
        p.grid_columnconfigure(0, weight=1)

        toolbar = ctk.CTkFrame(p, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(0, 2))
        ctk.CTkLabel(toolbar, text="Grafici", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(2, 12))
        self.btn_pause = ctk.CTkButton(toolbar, text="⏸ Pausa", width=90, command=self._toggle_pause)
        self.btn_pause.pack(side="left", padx=4)
        ctk.CTkButton(toolbar, text="Azzera", width=80, fg_color="gray40", hover_color="gray25", command=self._clear_plots).pack(side="left", padx=4)

        self.fig = Figure(figsize=(5, 6), dpi=100)
        self.fig.subplots_adjust(hspace=0.5, left=0.13, right=0.86, top=0.94, bottom=0.09)

        self.ax_slow = self.fig.add_subplot(211)
        self.ax_slow.set_title("Corrente DUT (log 10 Hz)", fontsize=10)
        self.ax_slow.set_xlabel("t [s]", fontsize=8)
        self.ax_slow.set_ylabel("I [mA]", fontsize=8)
        self.ax_slow.grid(True, alpha=0.3)
        self.ax_slow.tick_params(labelsize=8)
        (self.line_slow,) = self.ax_slow.plot([], [], color="#2563eb", lw=1.2, label="I")
        (self.line_thr,) = self.ax_slow.plot([], [], color="#cc0000", lw=1.0, ls="--", alpha=0.8, label="soglia (V_LIMIT)")
        self.ax_slow.legend(loc="upper right", fontsize=7)
        self.ax_slow_v = self.ax_slow.twinx()
        self.ax_slow_v.set_ylabel("V [V]", fontsize=8)
        self.ax_slow_v.tick_params(labelsize=8)

        self.ax_trace = self.fig.add_subplot(212)
        self.ax_trace.set_title("Ultima traccia evento", fontsize=10)
        self.ax_trace.set_xlabel("t [us]", fontsize=8)
        self.ax_trace.set_ylabel("I [mA]", fontsize=8)
        self.ax_trace.grid(True, alpha=0.3)
        self.ax_trace.tick_params(labelsize=8)
        (self.line_trace,) = self.ax_trace.plot([], [], color="#800080", lw=1.0)

        self.canvas = FigureCanvasTkAgg(self.fig, master=p)
        self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.canvas.draw()

    def _toggle_pause(self):
        self.plot_paused = not self.plot_paused
        self.btn_pause.configure(text="▶ Riprendi" if self.plot_paused else "⏸ Pausa",
                                 fg_color="green" if self.plot_paused else "#1f6aa5")

    def _clear_plots(self):
        self.slow_t.clear(); self.slow_i.clear(); self.slow_thr.clear()
        self.slow_t0 = None
        self.trace_x = []; self.trace_y = []
        self._trace_acc = []
        try:
            self.line_slow.set_data([], [])
            self.line_thr.set_data([], [])
            self.line_trace.set_data([], [])
            self.canvas.draw_idle()
        except Exception:
            pass

    def _rg_factor(self):
        try:
            return float(self.r_shunt.get()) * float(self.ina_gain.get()) / 1000.0
        except (ValueError, ZeroDivisionError):
            return 0.02

    def _threshold_mA(self):
        """Soglia I_TH [mA] dal valore DAC attivo (slider/preset)."""
        try:
            r = float(self.r_shunt.get()); g = float(self.ina_gain.get())
            v = self.cur_dac / DAC_MAX_COUNTS * VREF
            return v / (r * g) * 1000.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    def _refresh_plots(self):
        if self._plot_dirty and not self.plot_paused:
            self._plot_dirty = False
            try:
                if self.slow_t:
                    self.line_slow.set_data(list(self.slow_t), list(self.slow_i))
                    self.line_thr.set_data(list(self.slow_t), list(self.slow_thr))
                    self.ax_slow.relim(); self.ax_slow.autoscale_view()
                    lo, hi = self.ax_slow.get_ylim()
                    k = self._rg_factor()
                    self.ax_slow_v.set_ylim(lo * k, hi * k)
                if self.trace_x:
                    self.line_trace.set_data(self.trace_x, self.trace_y)
                    self.ax_trace.set_title(f"Ultima traccia evento — {self.trace_lbl}", fontsize=10)
                    self.ax_trace.relim(); self.ax_trace.autoscale_view()
                self.canvas.draw_idle()
            except Exception:
                pass
        self.after(400, self._refresh_plots)

    # ---------------------------------------------------------------- Log area
    def _build_log_area(self, parent):
        parent.grid_columnconfigure((0, 1), weight=1)
        parent.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(parent, text="Log Comunicazione TCP", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, pady=4)
        ctk.CTkLabel(parent, text="Log 10Hz & Event Traces", font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, pady=4)
        self.log = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Courier", size=12), state="disabled")
        self.log.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.slow_log = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Courier", size=12), state="disabled", fg_color="#F0F0F0")
        self.slow_log.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="nsew")
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
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            prefix = f"{self._run_prefix()}_{ts}"
            self.log_csv = open(f"{prefix}_log10hz.csv", "w")
            self.log_csv.write("PC_Time,Tick_ms,ADC_raw,Current_mA,Fresh,State,Retry,SEL,HCE\n")
            self.events_csv = open(f"{prefix}_events.csv", "w")
            self.events_csv.write("PC_Time,Event,Detail\n")
            self._log(f"File di run: {prefix}_*.csv", "info")
            self._log_event("CONNECT", f"{HOST}:{PORT}")
            self.slow_t.clear(); self.slow_i.clear(); self.slow_thr.clear(); self.slow_t0 = None
            # Protocollo v5: invia la config elettrica/parametrica al firmware
            self._send_config()
        except Exception as e:
            self.log_csv = None
            self.events_csv = None
            self._log(f"Errore apertura file di run: {e}", "err")

    def _on_disconnected(self):
        self._log_event("DISCONNECT")
        if self.log_csv:
            try: self.log_csv.close()
            except Exception: pass
            self.log_csv = None
        if self.events_csv:
            try: self.events_csv.close()
            except Exception: pass
            self.events_csv = None
        self.lbl_status.configure(text="● DISCONNESSO", text_color="red")
        self.btn_conn.configure(text="Connetti", fg_color=["#3B8ED0", "#1F6AA5"], hover_color=["#36719F", "#144870"])
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
                        # RTT misurato qui (arrivo pacchetto), non nel polling GUI
                        if line.startswith("PONG") and self.ping_sent_time is not None:
                            self._last_rtt = (time.time() - self.ping_sent_time) * 1000.0
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
        if self.permanent_off and cmd.strip().upper().startswith(("DUT_ON", "SWITCH ON")):
            if not messagebox.askyesno(
                    "DUT in FAULT",
                    "Il DUT e' in FAULT.\n\n"
                    "Il firmware rifiutera' l'accensione: usare ACK FAULT o RESET.\n"
                    "Inviare comunque?"):
                self._log("Accensione annullata (DUT in FAULT).", "info")
                return
        try:
            if cmd == "PING":
                self.ping_sent_time = time.time()
            self.sock.sendall((cmd + "\r\n").encode())
            self.tx_count += 1
            self.after(0, lambda: self.metric_tx.configure(text=str(self.tx_count)))
            if not cmd.startswith("DAC_SET"):
                self._log(f"-> {cmd}", "tx")
            if cmd.startswith(("DUT_ON", "DUT_OFF", "RESET", "TH_SELECT",
                               "TH_LOAD", "THOLD_SET", "TON_SET",
                               "INA_RST", "RETRY_SET", "TCLEAR_SET")):
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
                        fields = self._parse_kv(msg)
                        try:
                            st = int(fields.get("STATE", -1))
                            ret = int(fields["RETRY"]) if "RETRY" in fields else None
                            if st >= 0:
                                self._set_state_ui(st, ret)
                            if "SEL" in fields:
                                self.metric_sel.configure(text=fields["SEL"])
                            if "HCE" in fields:
                                self.metric_hce.configure(text=fields["HCE"])
                            if "I_MA" in fields or "I" in fields or "ADC" in fields:
                                # v5: ADC grezzo in ADC=, corrente in mA già pronta
                                # in I_MA= (calcolata dal firmware). Fallback al
                                # vecchio campo I= (conteggi) per retrocompat.
                                adc_raw = int(fields.get("ADC", fields.get("I", 0)))
                                if "I_MA" in fields:
                                    i_mA = float(fields["I_MA"])
                                else:
                                    try:
                                        i_mA = float(self._counts_to_mA(adc_raw))
                                    except ValueError:
                                        i_mA = 0.0
                                if "THR_MA" in fields:
                                    thr = float(fields["THR_MA"])
                                else:
                                    thr = self._threshold_mA()
                                now = time.time()
                                if self.slow_t0 is None:
                                    self.slow_t0 = now
                                try:
                                    self.slow_i.append(i_mA)
                                    self.slow_t.append(now - self.slow_t0)
                                    self.slow_thr.append(thr)
                                    self._plot_dirty = True
                                except ValueError:
                                    pass
                                if self.log_csv:
                                    pc_ts = time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int((now % 1) * 1000):03d}"
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
                        self.trace_active = True
                        self.trace_sample_idx = 0
                        self._trace_acc = []
                        _tk = msg.split()
                        self.trace_lbl = _tk[1] if len(_tk) > 1 else "EVT"
                        fields = self._parse_kv(msg)
                        try:
                            self.trace_fs = int(fields.get("FS", DEFAULT_FS))
                        except Exception:
                            self.trace_fs = DEFAULT_FS
                        self._log_slow(f"--- {msg} ---", "trace")
                        try:
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            ev = self.trace_lbl
                            fname = f"{self._run_prefix()}_{ts}_trace_{ev}.csv"
                            self.trace_file = open(fname, "w")
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
                        if self._trace_acc:
                            self.trace_x = [pt[0] for pt in self._trace_acc]
                            self.trace_y = [pt[1] for pt in self._trace_acc]
                            self._plot_dirty = True
                        if self.trace_file:
                            self.trace_file.close()
                            self.trace_file = None
                        continue
                    elif self.trace_active:
                        try:
                            parts = msg.split(",")
                            if len(parts) == 2:
                                idx = int(parts[0]); adc_raw = int(parts[1])
                                time_us = idx * 1e6 / self.trace_fs
                                i_mA = self._counts_to_mA(adc_raw)
                                try:
                                    self._trace_acc.append((time_us, float(i_mA)))
                                except ValueError:
                                    pass
                                if self.trace_file:
                                    self.trace_file.write(f"{time_us:.1f},{adc_raw},{i_mA}\n")
                                self.trace_sample_idx += 1
                            elif self.trace_file:
                                self.trace_file.write(f"# {msg}\n")
                        except Exception:
                            pass
                        continue

                    if msg == "PONG" or msg.startswith("PONG"):
                        if self._last_rtt is not None:
                            self.ping_sent_time = None
                            self.metric_ping.configure(text=f"{self._last_rtt:.1f} ms")
                            self._log(f"<- {msg}  [{self._last_rtt:.1f} ms]", "rx")
                            self._last_rtt = None
                        else:
                            self._log(f"<- {msg}", "rx")
                        self.rx_count += 1
                        self.metric_rx.configure(text=str(self.rx_count))
                        continue

                    self.rx_count += 1
                    self.metric_rx.configure(text=str(self.rx_count))

                    if msg.startswith("OK STATUS="):
                        f = self._parse_kv(msg)
                        try:
                            self._set_state_ui(f.get("STATUS", "—"),
                                               int(f["RETRY"]) if "RETRY" in f else None)
                        except Exception:
                            pass

                    if msg.startswith("DAC="):
                        try:
                            counts = int(msg.split("=")[1])
                            self.cur_dac = counts
                            self.metric_dac.configure(text=str(counts))
                            self.metric_dacv.configure(text=f"{counts / DAC_MAX_COUNTS * VREF:.2f} V")
                        except Exception:
                            pass

                    if msg.startswith("DAC_SET="):
                        try:
                            self.cur_dac = int(msg.split("=")[1])
                        except Exception:
                            pass

                    if msg.startswith("TH_SELECT="):
                        try:
                            f2 = self._parse_kv(msg)
                            self.cur_dac = int(f2.get("DAC", self.cur_dac))
                            self.metric_dac.configure(text=str(self.cur_dac))
                            self.metric_dacv.configure(text=f"{self.cur_dac / DAC_MAX_COUNTS * VREF:.2f} V")
                        except Exception:
                            pass

                    if not msg.startswith("DAC_SET"):
                        self._log(f"<- {msg}", "rx")
                elif kind == "err":
                    self._log(msg, "err")
        except queue.Empty:
            pass
        self.after(40, self._poll_rx_queue)

    # ============================================================ HELPERS
    @staticmethod
    def _parse_kv(msg):
        out = {}
        for tok in msg.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                out[k] = v
        return out

    def _set_state_ui(self, state, retry=None):
        if isinstance(state, int):
            name = STATE_NAMES[state] if 0 <= state < len(STATE_NAMES) else str(state)
        else:
            name = state or "—"
        if name in ("FAULT", "MANUAL_OFF"):
            color = "#cc0000"
        elif name in ("ALARM", "HOLD_RUN", "HCE_SAVE", "CUTOFF", "TON_RUN",
                      "RECOVERY", "VERIFY"):
            color = "#b35900"
        else:
            color = "black"
        self.metric_state.configure(text=name, text_color=color)
        if retry is not None:
            try:
                nmax = int(self.retry_max.get())
            except (ValueError, AttributeError):
                nmax = SEL_RETRY_MAX
            self.metric_retries.configure(text=f"{retry}/{nmax}",
                                          text_color="#cc0000" if retry >= nmax else "black")
        self._set_permanent_off(name == "FAULT")

    def _set_permanent_off(self, perm):
        if perm == self.permanent_off:
            return
        self.permanent_off = perm
        if perm:
            self.btn_dut_on.configure(state="disabled")
            self.lbl_perm_warn.configure(
                text="⚠  FAULT — DUT spento (retry di recovery esauriti o ALERT "
                     "bloccato).\nUsare ACK FAULT (o RESET) per riabilitare.")
            self.lbl_perm_warn.pack(fill="x", pady=(6, 0))
            self._log("Stato FAULT: accensione bloccata. Usare ACK FAULT o RESET.", "err")
        else:
            self.btn_dut_on.configure(state="normal")
            self.lbl_perm_warn.pack_forget()

    def _counts_to_mA(self, adc_raw):
        try:
            r = float(self.r_shunt.get()); g = float(self.ina_gain.get())
            v_adc = (adc_raw / ADC_MAX_COUNTS) * VREF
            return f"{(v_adc / (r * g)) * 1000.0:.3f}"
        except (ValueError, ZeroDivisionError):
            return ""

    def _run_prefix(self):
        def clean(s):
            s = s.strip().replace(" ", "-")
            s = "".join(ch for ch in s if ch.isalnum() or ch in "-_.")
            return s or "NA"
        return f"{clean(self.dut_id.get())}_{clean(self.let_id.get())}_{clean(self.run_id.get())}"

    def _log_event(self, event, detail=""):
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
                self._send_cmd("PING")
            except Exception as e:
                self.rx_queue.put(("err", f"Ping loop error: {e}"))
                self.ping_loop_active = False
                break
            time.sleep(1.0)

    # ================================================================== LOG
    def _log(self, msg, tag="info"):
        self.after(0, lambda: self._log_main_thread(msg, tag))

    def _log_main_thread(self, msg, tag):
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] {msg}\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_slow(self, msg, tag=None):
        self.after(0, lambda: self._log_slow_main_thread(msg, tag))

    def _log_slow_main_thread(self, msg, tag):
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
