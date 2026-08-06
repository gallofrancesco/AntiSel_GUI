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

# Collegamento RTU/PID (Figura 1, rif. RD04): dispositivi non ancora
# disponibili in laboratorio, IP/porta e protocollo sono TBD (§8.4).
# Placeholder in stile testuale GET/SET/OK, coerente con quello della Nucleo,
# pensato per essere sostituito quando le specifiche reali (o Modbus TCP)
# saranno definite.
RTU_HOST    = "192.168.1.101"
RTU_PORT    = 7756
RTU_TIMEOUT = 3.0
RTU_POLL_S  = 1.0

DAC_MAX_COUNTS = 4095    # DAC della Nucleo: 12 bit
ADC_MAX_COUNTS = 65535   # ADC della Nucleo: 16 bit (ADC_RESOLUTION_16B)
DEFAULT_FS     = 100000  # sample rate ADC [Sa/s], sovrascritto dall'header traccia
VREF           = 3.3

# Nomi stati allineati al firmware Fase 2 (macchina a 11 stati)
STATE_NAMES = ["INIT", "IDLE", "ALARM", "HOLD_RUN", "HCE_SAVE", "CUTOFF",
               "TON_RUN", "RECOVERY", "VERIFY", "MANUAL_OFF", "FAULT"]
SEL_RETRY_MAX = 3

I_TH_MIN, I_TH_MAX = 1.0, 50.0   # range soglia (R-02)

# ---------------------------------------------------------------- Design tokens
# Palette semantica dei pulsanti/stati, usata ovunque al posto di colori
# letterali sparsi, per coerenza visiva fra le sezioni.
CLR_OK             = "#15803d"
CLR_OK_HOVER       = "#0f5c2c"
CLR_DANGER         = "#dc2626"
CLR_DANGER_HOVER   = "#a91d1d"
CLR_WARN           = "#b45309"
CLR_WARN_HOVER     = "#8a3d06"
CLR_NEUTRAL        = "#52525b"
CLR_NEUTRAL_HOVER  = "#3f3f46"
CLR_MUTED          = "gray45"

# Accenti/tinte delle card per sezione: rosso = protezione/sicurezza DUT
# (azioni critiche), blu = configurazione, grigio = informazioni/stato.
CLR_ACCENT_PROTECT = "#dc2626"
CLR_ACCENT_CONFIG  = "#1f6aa5"
CLR_ACCENT_INFO    = "#52525b"
CLR_CARD_PROTECT   = ("#fdf1f1", "#3a1f1f")
CLR_CARD_CONFIG    = ("#f2f7fc", "#1c2733")
CLR_CARD_INFO      = ("#f2f2f3", "#232326")


def voltage_to_counts(v, vref=VREF):
    return int(max(0, min(DAC_MAX_COUNTS, round(v / vref * DAC_MAX_COUNTS))))


class AntiSELDashboard(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("Light")
        ctk.set_default_color_theme("blue")

        self.title("AntiSEL Dashboard")
        self.minsize(1560, 780)
        # Dimensione iniziale: NON usare winfo_screenwidth/height() per il
        # 100% del calcolo — su setup multi-monitor Tkinter riporta le
        # dimensioni del desktop virtuale COMBINATO (tutti i monitor
        # insieme), non del singolo schermo su cui la finestra apparira'.
        # Usarlo per dimensionare la finestra puo' produrla piu' alta di
        # qualunque monitor reale, spingendo l'area di log fuori dallo
        # schermo. Si usa quindi un tetto massimo assoluto sicuro (adatto a
        # un monitor singolo tipico) e si lascia decidere la posizione al
        # window manager, invece di calcolarla sullo schermo virtuale.
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = max(1560, min(int(screen_w * 0.9), 1850))
        win_h = max(780, min(int(screen_h * 0.85), 980))
        self.geometry(f"{win_w}x{win_h}")

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

        # Stato link RTU/PID (Figura 1, placeholder — vedi costanti RTU_*)
        self.rtu_sock        = None
        self.rtu_connected   = False
        self.rtu_rx_queue    = queue.Queue()
        self.rtu_rx_thread   = None
        self.rtu_poll_active = False

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

        # Variabili link RTU/PID
        self.rtu_host_val  = tk.StringVar(value=RTU_HOST)
        self.rtu_port_val  = tk.StringVar(value=str(RTU_PORT))
        self.setpoint_val  = tk.StringVar(value="85.0")   # setpoint T_DUT [°C] (rif. AD2 §3)

        # Buffer grafici
        self.slow_t   = deque(maxlen=600)
        self.slow_i   = deque(maxlen=600)
        self.slow_thr = deque(maxlen=600)
        self.slow_t0  = None
        self._trace_acc = []
        self.trace_x  = []
        self.trace_y  = []
        self.trace_lbl = ""
        # Innesto degli eventi (traccia ad alta risoluzione) sul grafico
        # continuo: ogni evento e' un segmento (x,y) in secondi sullo stesso
        # asse temporale del log 10 Hz, cosi' compare nel punto giusto.
        self._trace_overlay_acc = []
        self.trace_overlay_segments = deque(maxlen=20)
        self.trace_overlay_t0 = None
        self._plot_dirty = False
        self.plot_paused = False

        # Buffer grafico temperatura (link RTU/PID, placeholder)
        self.rtu_t    = deque(maxlen=600)
        self.rtu_temp = deque(maxlen=600)
        self.rtu_sp   = deque(maxlen=600)
        self.rtu_t0   = None

        self._build_ui()
        self.after(40, self._poll_rx_queue)
        self.after(40, self._poll_rtu_queue)
        self.after(400, self._refresh_plots)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================ UI
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=215)   # rete Nucleo
        self.grid_columnconfigure(1, weight=2, minsize=500)   # controlli DUT/AntiSEL (si allarga)
        # uniform="charts": forza colonna grafici e colonna RTU/PID alla
        # stessa larghezza esatta, indipendentemente da quanto richiedono i
        # rispettivi contenuti (altrimenti la larghezza "naturale" del
        # canvas matplotlib in col. 3 vince e sbilancia le due colonne).
        self.grid_columnconfigure(2, weight=2, minsize=380, uniform="charts")   # grafici corrente/traccia
        self.grid_columnconfigure(3, weight=2, minsize=380, uniform="charts")   # RTU/PID + temperatura
        self.grid_rowconfigure(0, weight=3)
        self.grid_rowconfigure(1, weight=1)                   # log

        self.col_left  = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color=("gray90", "gray14"))
        self.col_left.grid(row=0, column=0, sticky="nsew")
        self.col_mid   = ctk.CTkScrollableFrame(self, label_text="Controlli AntiSEL",
                                                 label_font=ctk.CTkFont(size=14, weight="bold"))
        self.col_mid.grid(row=0, column=1, sticky="nsew", padx=(6, 3), pady=6)
        self.col_right = ctk.CTkFrame(self, fg_color="transparent")
        self.col_right.grid(row=0, column=2, sticky="nsew", padx=(3, 3), pady=6)
        self.col_rtu   = ctk.CTkFrame(self, width=380, corner_radius=0, fg_color="transparent")
        self.col_rtu.grid(row=0, column=3, sticky="nsew", pady=6)

        self._build_left(self.col_left)
        self._build_center(self.col_mid)
        self._build_charts(self.col_right)
        self._build_rtu_panel(self.col_rtu)

        self.log_frame = ctk.CTkFrame(self)
        self.log_frame.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=6, pady=(0, 6))
        self._build_log_area(self.log_frame)

    def _add_metric(self, parent, row, label, value, col=0):
        """col seleziona la coppia di colonne (label, valore): col=0 -> 0/1,
        col=1 -> 2/3, ecc. Permette di affiancare piu' metriche sulla stessa
        riga invece di impilarle (ogni riga in piu' costa altezza preziosa)."""
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(size=11, weight="bold")).grid(row=row, column=col * 2, padx=10, pady=2, sticky="e")
        val_lbl = ctk.CTkLabel(parent, text=value, font=ctk.CTkFont(size=12))
        val_lbl.grid(row=row, column=col * 2 + 1, padx=10, pady=2, sticky="w")
        return val_lbl

    # ---------------------------------------------------------------- Colonna SX
    def _build_left(self, p):
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(7, weight=1)

        ctk.CTkLabel(p, text="AntiSEL\nControl", font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, padx=20, pady=(18, 8))

        self.lbl_status = ctk.CTkLabel(p, text="● DISCONNESSO", text_color="white", fg_color=CLR_DANGER,
                                        corner_radius=6, font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_status.grid(row=1, column=0, padx=20, pady=4, sticky="ew")
        self.lbl_target = ctk.CTkLabel(p, text=f"{HOST}:{PORT}", text_color=CLR_MUTED)
        self.lbl_target.grid(row=2, column=0, padx=20, pady=(2, 0))
        self.btn_conn = ctk.CTkButton(p, text="Connetti", command=self._toggle_connection)
        self.btn_conn.grid(row=3, column=0, padx=20, pady=12)

        mf = ctk.CTkFrame(p, fg_color=("white", "gray20"), corner_radius=8)
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

    # ---------------------------------------------------------------- Colonna DX (RTU/PID)
    def _build_rtu_panel(self, p):
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(1, weight=1)   # stessa proporzione di col_right riga 1

        # --- Grafico temperatura: stessa struttura (toolbar + canvas) e
        # stesso padding della colonna grafici, cosi' il canvas risulta
        # allineato in alto con "Corrente DUT" (col_right, riga 0/1). ---
        toolbar = ctk.CTkFrame(p, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(0, 2))
        ctk.CTkLabel(toolbar, text="Temperatura", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(2, 0))

        # body: chart (riga 0) + resto dei controlli RTU/PID (riga 1), pesi
        # uguali cosi' il grafico occupa esattamente META' dell'altezza
        # disponibile in questa riga — la stessa altezza di ciascuno dei due
        # subplot impilati in col_right (che condividono la stessa riga 1).
        body = ctk.CTkFrame(p, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        # uniform="rtu_split": stessa logica di cui sopra, forza il grafico
        # (riga 0) e il resto dei controlli (riga 1) alla stessa altezza,
        # cosi' il grafico risulta esattamente meta' della riga — la stessa
        # altezza di ciascuno dei due subplot impilati in col_right.
        body.grid_rowconfigure(0, weight=1, uniform="rtu_split")
        body.grid_rowconfigure(1, weight=1, uniform="rtu_split")

        self.fig_rtu_temp = Figure(figsize=(3.6, 3.0), dpi=100)
        self.fig_rtu_temp.subplots_adjust(left=0.18, right=0.95, top=0.90, bottom=0.16)
        self.ax_temp = self.fig_rtu_temp.add_subplot(111)
        self.ax_temp.set_title("T_DUT (RTU/PID)", fontsize=9)
        self.ax_temp.set_xlabel("t [s]", fontsize=7)
        self.ax_temp.set_ylabel("T [°C]", fontsize=7)
        self.ax_temp.grid(True, alpha=0.3)
        self.ax_temp.tick_params(labelsize=7)
        (self.line_temp,) = self.ax_temp.plot([], [], color="#c2410c", lw=1.2, label="T_DUT (misurata)")
        (self.line_setpoint,) = self.ax_temp.plot([], [], color="#666666", lw=1.0, ls="--", alpha=0.8, label="Setpoint")
        self.ax_temp.legend(loc="upper right", fontsize=6)

        self.canvas_rtu_temp = FigureCanvasTkAgg(self.fig_rtu_temp, master=body)
        self.canvas_rtu_temp.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.canvas_rtu_temp.draw()

        ctrl = ctk.CTkFrame(body, fg_color="transparent")
        ctrl.grid(row=1, column=0, sticky="nsew")
        ctrl.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ctrl, text="PID CTRL + RTU", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, padx=15, pady=(6, 0))
        ctk.CTkLabel(ctrl, text="(placeholder — TBD)", font=ctk.CTkFont(size=10), text_color="gray50").grid(row=1, column=0, padx=15)

        self.lbl_rtu_status = ctk.CTkLabel(ctrl, text="● non connesso", text_color="white", fg_color=CLR_DANGER,
                                           corner_radius=6, font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_rtu_status.grid(row=2, column=0, padx=15, pady=(6, 3), sticky="ew")

        addr = ctk.CTkFrame(ctrl, fg_color="transparent")
        addr.grid(row=3, column=0, padx=15, pady=1, sticky="ew")
        ctk.CTkLabel(addr, text="IP:").grid(row=0, column=0, padx=(0, 3), sticky="e")
        ctk.CTkEntry(addr, textvariable=self.rtu_host_val, width=100).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(addr, text="Porta:").grid(row=1, column=0, padx=(0, 3), sticky="e")
        ctk.CTkEntry(addr, textvariable=self.rtu_port_val, width=60).grid(row=1, column=1, sticky="w")

        self.btn_rtu_conn = ctk.CTkButton(ctrl, text="Connetti", command=self._rtu_toggle_connection)
        self.btn_rtu_conn.grid(row=4, column=0, padx=15, pady=6)

        mf = ctk.CTkFrame(ctrl, fg_color=("white", "gray20"), corner_radius=8)
        mf.grid(row=5, column=0, padx=15, pady=3, sticky="ew")
        self.metric_temp      = self._add_metric(mf, 0, "T_DUT [°C]", "—")
        self.metric_pwm       = self._add_metric(mf, 1, "PWM PID [%]", "—")
        self.metric_pid_state = self._add_metric(mf, 2, "Stato PID", "—")

        sp = ctk.CTkFrame(ctrl, fg_color="transparent")
        sp.grid(row=6, column=0, padx=15, pady=(4, 2), sticky="ew")
        sp.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(sp, text="Setpoint (°C):").grid(row=0, column=0, columnspan=2, sticky="w")
        ctk.CTkEntry(sp, textvariable=self.setpoint_val, width=70, justify="center").grid(row=1, column=0, padx=(0, 4), pady=(2, 0), sticky="ew")
        ctk.CTkButton(sp, text="Set", width=60, command=self._rtu_set_setpoint).grid(row=1, column=1, pady=(2, 0), sticky="ew")

    # ---------------------------------------------------------------- Colonna CENTRO
    def _build_center(self, p):
        p.grid_columnconfigure(0, weight=1)
        r = 0

        # --- Azioni DUT ---
        sec = self._section(p, r, "Azioni DUT", kind="protect"); r += 1
        act = ctk.CTkFrame(sec, fg_color="transparent"); act.pack(fill="x")
        self.btn_dut_on = ctk.CTkButton(act, text="DUT ON", fg_color=CLR_OK, hover_color=CLR_OK_HOVER, command=lambda: self._send_cmd("DUT_ON"))
        self.btn_dut_on.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_dut_off = ctk.CTkButton(act, text="DUT OFF", fg_color=CLR_DANGER, hover_color=CLR_DANGER_HOVER, command=lambda: self._send_cmd("DUT_OFF"))
        self.btn_dut_off.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_reset = ctk.CTkButton(act, text="RESET", fg_color=CLR_WARN, hover_color=CLR_WARN_HOVER, command=lambda: self._send_cmd("RESET"))
        self.btn_reset.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_ack = ctk.CTkButton(act, text="ACK FAULT", fg_color=CLR_WARN_HOVER, hover_color="#5c2a04", command=lambda: self._send_cmd("ACK FAULT"))
        self.btn_ack.pack(side="left", padx=4, expand=True, fill="x")
        self.lbl_perm_warn = ctk.CTkLabel(sec, text="", text_color=CLR_DANGER, justify="left", font=ctk.CTkFont(size=12, weight="bold"))

        # --- Latch INA301 ---
        sec = self._section(p, r, "Latch INA301 (policy riarmo)", kind="protect"); r += 1
        lr = ctk.CTkFrame(sec, fg_color="transparent"); lr.pack(fill="x")
        self.btn_ina_rst = ctk.CTkButton(lr, text="Reset allarme", width=110, fg_color=CLR_WARN, hover_color=CLR_WARN_HOVER, command=lambda: self._send_cmd("INA_RST"))
        self.btn_ina_rst.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(lr, text="N:").pack(side="left")
        ctk.CTkEntry(lr, textvariable=self.retry_max, width=42).pack(side="left", padx=(2, 4))
        ctk.CTkButton(lr, text="Set", width=40, command=lambda: self._send_cmd(f"SET RETRY_MAX {self.retry_max.get()}")).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(lr, text="T_CLEAR:").pack(side="left")
        ctk.CTkEntry(lr, textvariable=self.t_clear, width=52).pack(side="left", padx=(2, 4))
        ctk.CTkButton(lr, text="Set", width=40, command=lambda: self._send_cmd(f"SET TCLEAR_MS {self.t_clear.get()}")).pack(side="left")

        # --- Soglia I_TH (precisa) ---
        sec = self._section(p, r, "Soglia di corrente I_TH", kind="config"); r += 1
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
        self.lbl_ith_calc.pack(anchor="w", pady=(2, 0))
        # Preset (§8.2) — opzionali in questa fase
        pr = ctk.CTkFrame(sec, fg_color="transparent"); pr.pack(fill="x", pady=(3, 0))
        ctk.CTkLabel(pr, text="Preset:", font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        for n in (1, 2, 3):
            ctk.CTkButton(pr, text=f"Carica {n}", width=64, command=lambda n=n: self._th_load(n)).pack(side="left", padx=2)
            ctk.CTkButton(pr, text=f"Usa {n}", width=48, fg_color=CLR_NEUTRAL, hover_color=CLR_NEUTRAL_HOVER, command=lambda n=n: self._send_cmd(f"TH_SELECT {n}")).pack(side="left", padx=(0, 6))

        # --- Tempistiche ---
        sec = self._section(p, r, "Tempistiche", kind="config"); r += 1
        tr = ctk.CTkFrame(sec, fg_color="transparent"); tr.pack(fill="x")
        ctk.CTkLabel(tr, text="T_HOLD (ms):").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(tr, textvariable=self.thold_val, width=60).pack(side="left")
        ctk.CTkButton(tr, text="Set", width=44, command=self._set_thold).pack(side="left", padx=(4, 16))
        ctk.CTkLabel(tr, text="T_ON (ms):").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(tr, textvariable=self.ton_val, width=60).pack(side="left")
        ctk.CTkButton(tr, text="Set", width=44, command=self._set_ton).pack(side="left", padx=(4, 0))

        # --- Hardware ---
        sec = self._section(p, r, "Hardware", kind="config"); r += 1
        hr = ctk.CTkFrame(sec, fg_color="transparent"); hr.pack(fill="x")
        ctk.CTkLabel(hr, text="R_SHUNT (Ω):").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(hr, textvariable=self.r_shunt, width=70).pack(side="left", padx=(0, 16))
        ctk.CTkLabel(hr, text="Gain INA301:").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(hr, textvariable=self.ina_gain, width=70).pack(side="left", padx=(0, 6))
        ctk.CTkButton(hr, text="Set", width=44, command=self._set_hw).pack(side="left", padx=(4, 0))

        # --- Run ---
        sec = self._section(p, r, "Run (nomi file CSV)", kind="config"); r += 1
        rr = ctk.CTkFrame(sec, fg_color="transparent"); rr.pack(fill="x")
        for lbl, var, w in (("DUT id", self.dut_id, 110), ("LET", self.let_id, 70), ("Run id", self.run_id, 90)):
            ctk.CTkLabel(rr, text=f"{lbl}:").pack(side="left", padx=(0, 3))
            ctk.CTkEntry(rr, textvariable=var, width=w).pack(side="left", padx=(0, 8))

        # --- Stato MCU ---
        sec = self._section(p, r, "Stato", kind="info"); r += 1
        st = ctk.CTkFrame(sec, fg_color="transparent"); st.pack(fill="x")
        st.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6, 7), weight=1)
        self.metric_state   = self._add_metric(st, 0, "Stato MCU", "—", col=0)
        self.metric_retries = self._add_metric(st, 0, "Tentativi", f"0/{SEL_RETRY_MAX}", col=1)
        self.metric_sel     = self._add_metric(st, 0, "SEL", "0", col=2)
        self.metric_hce     = self._add_metric(st, 0, "HCE", "0", col=3)
        st2 = ctk.CTkFrame(sec, fg_color="transparent"); st2.pack(fill="x")
        st2.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.metric_dac  = self._add_metric(st2, 0, "DAC read", "— counts", col=0)
        self.metric_dacv = self._add_metric(st2, 0, "Volt read", "— V", col=1)

        self._apply_ith()  # inizializza lbl_ith_calc + cur_dac

    _SECTION_STYLE = {
        "protect": (CLR_ACCENT_PROTECT, CLR_CARD_PROTECT),
        "config":  (CLR_ACCENT_CONFIG,  CLR_CARD_CONFIG),
        "info":    (CLR_ACCENT_INFO,    CLR_CARD_INFO),
    }

    def _section(self, parent, row, title, kind="config"):
        """Card di sezione con barra accento colorata (gerarchia visiva):
        rosso = azioni/protezione DUT, blu = configurazione, grigio = stato."""
        accent, bg = self._SECTION_STYLE.get(kind, self._SECTION_STYLE["config"])
        outer = ctk.CTkFrame(parent, fg_color=bg, corner_radius=8)
        outer.grid(row=row, column=0, sticky="ew", padx=5, pady=6)
        outer.grid_columnconfigure(1, weight=1)
        # height=1: senza override esplicito CTkFrame usa un'altezza di
        # default di 200px che, essendo gridata (non "place"), farebbe
        # crescere l'intera card a 200px indipendentemente dal contenuto.
        bar = ctk.CTkFrame(outer, fg_color=accent, width=4, height=1, corner_radius=0)
        bar.grid(row=0, column=0, rowspan=2, sticky="ns")
        ctk.CTkLabel(outer, text=title.upper(), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=accent).grid(row=0, column=1, sticky="w", padx=12, pady=(8, 3))
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.grid(row=1, column=1, sticky="ew", padx=12, pady=(0, 9))
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

    def _set_hw(self):
        """Invia R_SHUNT/Gain al firmware e ricalcola la soglia I_TH corrente
        (protocollo v5): senza questo Set i valori restano solo locali fino
        alla prossima connessione (_send_config)."""
        try:
            r = float(self.r_shunt.get()); g = float(self.ina_gain.get())
        except ValueError:
            self._log("R_SHUNT/Gain non validi.", "err")
            return
        self._send_cmd(f"SET GAIN {int(g)}")
        self._send_cmd(f"SET RSHUNT {r:.3f}")
        self._apply_ith()

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
        ctk.CTkButton(toolbar, text="Azzera", width=80, fg_color=CLR_NEUTRAL, hover_color=CLR_NEUTRAL_HOVER, command=self._clear_plots).pack(side="left", padx=4)

        self.fig = Figure(figsize=(3.8, 6), dpi=100)
        self.fig.subplots_adjust(hspace=0.5, left=0.13, right=0.86, top=0.94, bottom=0.09)

        self.ax_slow = self.fig.add_subplot(211)
        self.ax_slow.set_title("Corrente DUT (log 10 Hz)", fontsize=10)
        self.ax_slow.set_xlabel("t [s]", fontsize=8)
        self.ax_slow.set_ylabel("I [mA]", fontsize=8)
        self.ax_slow.grid(True, alpha=0.3)
        self.ax_slow.tick_params(labelsize=8)
        (self.line_slow,) = self.ax_slow.plot([], [], color="#2563eb", lw=1.2, label="I")
        (self.line_thr,) = self.ax_slow.plot([], [], color=CLR_DANGER, lw=1.0, ls="--", alpha=0.8, label="soglia (V_LIMIT)")
        (self.line_slow_trace,) = self.ax_slow.plot([], [], color="#800080", lw=1.3, label="evento (100 kSa/s)")
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
        self.trace_overlay_segments.clear()
        self._trace_overlay_acc = []
        self.trace_overlay_t0 = None
        self.rtu_t.clear(); self.rtu_temp.clear(); self.rtu_sp.clear()
        self.rtu_t0 = None
        try:
            self.line_slow.set_data([], [])
            self.line_thr.set_data([], [])
            self.line_trace.set_data([], [])
            self.line_slow_trace.set_data([], [])
            self.canvas.draw_idle()
        except Exception:
            pass
        try:
            self.line_temp.set_data([], [])
            self.line_setpoint.set_data([], [])
            self.canvas_rtu_temp.draw_idle()
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
                    # Scarta i segmenti-evento usciti dalla finestra visibile del
                    # log 10 Hz (altrimenti un vecchio evento tiene allargato
                    # l'asse x anche quando il log corrente e' andato avanti).
                    t_min = self.slow_t[0]
                    while self.trace_overlay_segments and self.trace_overlay_segments[0][-1][0] < t_min:
                        self.trace_overlay_segments.popleft()
                    ox, oy = [], []
                    for seg in self.trace_overlay_segments:
                        if ox:
                            ox.append(float("nan")); oy.append(float("nan"))
                        ox.extend(p[0] for p in seg)
                        oy.extend(p[1] for p in seg)
                    self.line_slow_trace.set_data(ox, oy)
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
            try:
                if self.rtu_t:
                    self.line_temp.set_data(list(self.rtu_t), list(self.rtu_temp))
                    self.line_setpoint.set_data(list(self.rtu_t), list(self.rtu_sp))
                    self.ax_temp.relim(); self.ax_temp.autoscale_view()
                    self.canvas_rtu_temp.draw_idle()
            except Exception:
                pass
        self.after(400, self._refresh_plots)

    # ---------------------------------------------------------------- Log area
    def _build_log_area(self, parent):
        parent.grid_columnconfigure((0, 1), weight=1)
        parent.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(parent, text="LOG COMUNICAZIONE TCP", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=CLR_ACCENT_CONFIG).grid(row=0, column=0, pady=(8, 4))
        ctk.CTkLabel(parent, text="LOG 10 HZ & EVENT TRACES", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=CLR_ACCENT_CONFIG).grid(row=0, column=1, pady=(8, 4))
        self.log = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Courier", size=12), state="disabled", corner_radius=8)
        self.log.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.slow_log = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Courier", size=12), state="disabled",
                                        fg_color="#F0F0F0", corner_radius=8)
        self.slow_log.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="nsew")
        self.log.tag_config("tx", foreground=CLR_ACCENT_CONFIG)
        self.log.tag_config("rx", foreground=CLR_OK)
        self.log.tag_config("info", foreground=CLR_WARN)
        self.log.tag_config("err", foreground=CLR_DANGER)
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
        self.lbl_status.configure(text="● CONNESSO", fg_color=CLR_OK)
        self.btn_conn.configure(text="Disconnetti", fg_color=CLR_DANGER, hover_color=CLR_DANGER_HOVER)
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
        self.lbl_status.configure(text="● DISCONNESSO", fg_color=CLR_DANGER)
        self.btn_conn.configure(text="Connetti", fg_color=["#3B8ED0", "#1F6AA5"], hover_color=["#36719F", "#144870"])
        self.btn_ping_loop.configure(fg_color="transparent")
        self._log("Disconnesso.", "info")

    # ============================================================ RTU/PID (Figura 1, placeholder)
    def _rtu_toggle_connection(self):
        if self.rtu_connected:
            self._rtu_disconnect()
        else:
            threading.Thread(target=self._rtu_connect, daemon=True).start()

    def _rtu_connect(self):
        try:
            host = self.rtu_host_val.get().strip()
            port = int(self.rtu_port_val.get())
        except ValueError:
            self.rtu_rx_queue.put(("err", "RTU/PID: IP o porta non validi."))
            return
        self._log(f"RTU/PID: connessione a {host}:{port}...", "info")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(RTU_TIMEOUT)
            s.connect((host, port))
            s.settimeout(None)
            self.rtu_sock = s
            self.rtu_connected = True
            self.after(0, self._rtu_on_connected)
            self.rtu_rx_thread = threading.Thread(target=self._rtu_rx_loop, daemon=True)
            self.rtu_rx_thread.start()
            self.rtu_poll_active = True
            threading.Thread(target=self._rtu_poll_loop, daemon=True).start()
        except Exception as e:
            self.rtu_rx_queue.put(("err", f"RTU/PID: connessione fallita: {e}"))

    def _rtu_disconnect(self):
        self.rtu_poll_active = False
        self.rtu_connected   = False
        if self.rtu_sock:
            try:
                self.rtu_sock.shutdown(socket.SHUT_RDWR)
                self.rtu_sock.close()
            except Exception:
                pass
            self.rtu_sock = None
        self._rtu_on_disconnected()

    def _rtu_on_connected(self):
        self.lbl_rtu_status.configure(text="● connesso", fg_color=CLR_OK)
        self.btn_rtu_conn.configure(text="Disconnetti RTU/PID", fg_color=CLR_DANGER, hover_color=CLR_DANGER_HOVER)
        self.rtu_t.clear(); self.rtu_temp.clear(); self.rtu_sp.clear()
        self.rtu_t0 = None
        self._log("RTU/PID: connesso.", "info")

    def _rtu_on_disconnected(self):
        self.lbl_rtu_status.configure(text="● non connesso", fg_color=CLR_DANGER)
        self.btn_rtu_conn.configure(text="Connetti RTU/PID", fg_color=["#3B8ED0", "#1F6AA5"], hover_color=["#36719F", "#144870"])
        self.metric_temp.configure(text="—")
        self.metric_pwm.configure(text="—")
        self.metric_pid_state.configure(text="—")
        self._log("RTU/PID: disconnesso.", "info")

    def _rtu_rx_loop(self):
        try:
            buffer = b""
            while self.rtu_connected:
                chunk = self.rtu_sock.recv(256)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode().strip()
                    if line:
                        self.rtu_rx_queue.put(("rx", line))
        except Exception:
            pass
        finally:
            if self.rtu_connected:
                self.rtu_rx_queue.put(("err", "RTU/PID: connessione persa."))
                self.rtu_connected = False
                self.after(0, self._rtu_on_disconnected)

    def _rtu_send_cmd(self, cmd):
        if not self.rtu_connected or not self.rtu_sock:
            return
        try:
            self.rtu_sock.sendall((cmd + "\r\n").encode())
            self._log(f"RTU/PID -> {cmd}", "tx")
        except Exception as e:
            self._log(f"RTU/PID errore TX: {e}", "err")

    def _rtu_set_setpoint(self):
        try:
            sp = float(self.setpoint_val.get())
        except ValueError:
            return
        self._rtu_send_cmd(f"SET SETPOINT_C {sp:.1f}")

    def _rtu_poll_loop(self):
        """Interrogazione periodica di temperatura e stato PID (placeholder)."""
        while self.rtu_poll_active and self.rtu_connected:
            self._rtu_send_cmd("GET TEMP")
            self._rtu_send_cmd("GET PID")
            time.sleep(RTU_POLL_S)

    def _poll_rtu_queue(self):
        try:
            while True:
                kind, msg = self.rtu_rx_queue.get_nowait()
                if kind == "rx":
                    fields = self._parse_kv(msg)
                    if "TEMP" in fields:
                        try:
                            temp = float(fields["TEMP"])
                            self.metric_temp.configure(text=f"{temp:.1f}")
                            now = time.time()
                            if self.rtu_t0 is None:
                                self.rtu_t0 = now
                            try:
                                sp = float(self.setpoint_val.get())
                            except ValueError:
                                sp = float("nan")
                            self.rtu_t.append(now - self.rtu_t0)
                            self.rtu_temp.append(temp)
                            self.rtu_sp.append(sp)
                            self._plot_dirty = True
                        except ValueError:
                            pass
                    if "PWM" in fields:
                        try:
                            self.metric_pwm.configure(text=f"{float(fields['PWM']):.1f}")
                        except ValueError:
                            pass
                    if "STATE" in fields:
                        self.metric_pid_state.configure(text=fields["STATE"])
                    self._log(f"RTU/PID <- {msg}", "rx")
                elif kind == "err":
                    self._log(msg, "err")
        except queue.Empty:
            pass
        self.after(40, self._poll_rtu_queue)

    def _on_close(self):
        if self.connected:
            self._disconnect()
        if self.rtu_connected:
            self._rtu_disconnect()
        self.destroy()

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
                        self._trace_overlay_acc = []
                        # Ancora l'evento sullo stesso asse temporale (secondi
                        # dall'inizio) del log 10 Hz, cosi' l'innesto compare
                        # nel punto giusto sul grafico continuo.
                        self.trace_overlay_t0 = (time.time() - self.slow_t0) if self.slow_t0 is not None else None
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
                        if self._trace_overlay_acc:
                            self.trace_overlay_segments.append(self._trace_overlay_acc)
                            self._trace_overlay_acc = []
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
                                    if self.trace_overlay_t0 is not None:
                                        self._trace_overlay_acc.append(
                                            (self.trace_overlay_t0 + time_us / 1e6, float(i_mA))
                                        )
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
            color = CLR_DANGER
        elif name in ("ALARM", "HOLD_RUN", "HCE_SAVE", "CUTOFF", "TON_RUN",
                      "RECOVERY", "VERIFY"):
            color = CLR_WARN
        else:
            color = "gray10"
        self.metric_state.configure(text=name, text_color=color)
        if retry is not None:
            try:
                nmax = int(self.retry_max.get())
            except (ValueError, AttributeError):
                nmax = SEL_RETRY_MAX
            self.metric_retries.configure(text=f"{retry}/{nmax}",
                                          text_color=CLR_DANGER if retry >= nmax else "gray10")
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
