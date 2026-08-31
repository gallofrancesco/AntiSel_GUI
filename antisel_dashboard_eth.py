"""
AntiSEL Dashboard v4.5 — Ethernet TCP (CustomTkinter UI)
Comunicazione con NUCLEO-H755ZI-Q @ 192.168.1.100:7755

v4.5:
- Layout a 3 colonne: [rete] | [controlli] | [grafici sempre visibili]
- I_TH numerico e preciso (entry + step ±0.1/±1 mA), niente slider
- T_HOLD / T_ON come campi numerici
Storia precedente (firmware latched, discriminazione ADC, controlli latch,
grafici corrente/tensione + traccia) invariata.

Front-end puro: widget, layout e plotting. La comunicazione TCP con la
Nucleo e con il link RTU/PID, il protocollo, il logging su CSV e le
conversioni elettriche vivono in nucleo_client.py.
"""

import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
import threading
import time
import queue
from collections import deque
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.ticker as ticker
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from nucleo_client import (
    NucleoClient, RtuClient, parse_kv,
    voltage_to_counts, counts_to_mA,
    HOST, PORT, RTU_HOST, RTU_PORT,
    DAC_MAX_COUNTS, DEFAULT_FS, VREF,
    STATE_NAMES, SEL_RETRY_MAX, I_TH_MIN, I_TH_MAX,
)

# ---------------------------------------------------------------- Design tokens
# Tema scuro in stile analizzatore da banco (superfici quasi nere, pannelli
# leggermente rilevati, accenti categoriali). Hex dei ruoli e della palette
# categoriale/di stato dalla skill dataviz (references/palette.md, colonna
# "Dark"): validati per contrasto e distinguibilità daltonica su sfondo
# scuro — non scelti a occhio.
BG_PAGE            = "#0d0d0d"   # sfondo finestra
BG_PANEL           = "#1a1a19"   # canvas grafici, superficie card di base
BG_PANEL_RAISED    = "#202020"   # pannelli metriche (leggermente rilevati)
BG_SIDEBAR         = "#111110"   # colonna sinistra

INK_PRIMARY        = "#ffffff"
INK_SECONDARY      = "#c3c2b7"
INK_MUTED          = "#898781"
GRID_LINE          = "#2c2c2a"
AXIS_LINE          = "#383835"

# Palette semantica dei pulsanti/stati (status palette dataviz: good/warning/
# critical), usata ovunque al posto di colori letterali sparsi.
CLR_OK             = "#0ca30c"   # status "good"
CLR_OK_HOVER       = "#087f0a"
CLR_DANGER         = "#d03b3b"   # status "critical"
CLR_DANGER_HOVER   = "#a52f2f"
CLR_WARN           = "#c98500"   # status "warning" (step scuro, leggibile su bottone)
CLR_WARN_HOVER     = "#9c6900"
CLR_WARN_DEEP      = "#8a5c00"   # ACK FAULT: tono piu' cupo di CLR_WARN
CLR_WARN_DEEP_HOVER = "#5c3d00"
CLR_NEUTRAL        = "#52525b"
CLR_NEUTRAL_HOVER  = "#3f3f46"
CLR_MUTED          = INK_MUTED

# Accenti/tinte delle card per sezione: rosso = protezione/sicurezza DUT
# (azioni critiche), blu = configurazione, grigio = informazioni/stato.
CLR_ACCENT_PROTECT = CLR_DANGER
CLR_ACCENT_CONFIG  = "#3987e5"   # categorico "blue" (step scuro)
CLR_ACCENT_CONFIG_HOVER = "#2a78d6"
CLR_ACCENT_INFO    = CLR_NEUTRAL
CLR_CARD_PROTECT   = "#2a1c1c"
CLR_CARD_CONFIG    = "#16212c"
CLR_CARD_INFO      = "#1e1e1d"

# Palette categoriale (step scuri) per le serie dei grafici.
CLR_SERIES_BLUE    = "#3987e5"
CLR_SERIES_ORANGE  = "#d95926"
CLR_SERIES_AQUA    = "#199e70"
CLR_SERIES_VIOLET  = "#9085e9"
CLR_SERIES_RED     = "#e66767"


class AntiSELDashboard(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.title("AntiSEL Dashboard")
        self.configure(fg_color=BG_PAGE)
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
        win_w = max(1560, min(int(screen_w * 0.95), 1850))
        win_h = max(780, min(int(screen_h * 0.95), 1200))
        self.geometry(f"{win_w}x{win_h}")
        def _maximize():
            try:
                self.state("zoomed")
            except Exception:
                pass
        self.after(100, _maximize)

        # Client di comunicazione (nucleo_client.py)
        self.client     = NucleoClient(HOST, PORT)
        self.rtu_client = RtuClient(RTU_HOST, RTU_PORT)

        # Stato GUI
        self.ping_loop_active = False
        self.trace_active     = False
        self.trace_fs         = DEFAULT_FS
        self.trace_sample_idx = 0
        self.permanent_off    = False
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

        # Variabili link RTU/PID
        self.rtu_host_val  = tk.StringVar(value=RTU_HOST)
        self.rtu_port_val  = tk.StringVar(value=str(RTU_PORT))
        self.setpoint_val  = tk.StringVar(value="85.0")   # setpoint T_DUT [°C] (rif. AD2 §3)

        # Buffer grafici
        self.slow_t   = deque(maxlen=600)
        self.slow_i   = deque(maxlen=600)
        self.slow_v   = deque(maxlen=600)
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
        self._zoomed_slow = False
        self._zoomed_volt = False
        self._pan_start = None
        self._pan_axes = None
        self._pan_xlim = None
        self._pan_ylim = None

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

        self.col_left  = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color=BG_SIDEBAR)
        self.col_left.grid(row=0, column=0, sticky="nsew")
        self.col_mid   = ctk.CTkScrollableFrame(self, label_text="Controlli AntiSEL",
                                                 label_font=ctk.CTkFont(size=14, weight="bold"),
                                                 fg_color=BG_PAGE, label_fg_color=BG_PAGE)
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

    @staticmethod
    def _style_figure(fig):
        """Sfondo scuro per l'area della figura fuori dagli assi (margini)."""
        fig.patch.set_facecolor(BG_PANEL)

    @staticmethod
    def _style_axes(ax):
        """Tema scuro di un subplot: superficie, assi, tick e label."""
        ax.set_facecolor(BG_PANEL)
        ax.tick_params(colors=INK_SECONDARY)
        ax.xaxis.label.set_color(INK_SECONDARY)
        ax.yaxis.label.set_color(INK_SECONDARY)
        ax.title.set_color(INK_PRIMARY)
        for spine in ax.spines.values():
            spine.set_color(AXIS_LINE)
        ax.grid(True, alpha=0.7, color=GRID_LINE)

    @staticmethod
    def _style_legend(legend):
        legend.get_frame().set_facecolor(BG_PANEL_RAISED)
        legend.get_frame().set_edgecolor(AXIS_LINE)
        for text in legend.get_texts():
            text.set_color(INK_PRIMARY)
        return legend

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
        self.btn_conn = ctk.CTkButton(p, text="Connetti", fg_color=CLR_ACCENT_CONFIG, hover_color=CLR_ACCENT_CONFIG_HOVER, command=self._toggle_connection)
        self.btn_conn.grid(row=3, column=0, padx=20, pady=12)

        mf = ctk.CTkFrame(p, fg_color=BG_PANEL_RAISED, corner_radius=8)
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
        self._style_figure(self.fig_rtu_temp)
        self.ax_temp = self.fig_rtu_temp.add_subplot(111)
        self._style_axes(self.ax_temp)
        self.ax_temp.set_title("T_DUT (RTU/PID)", fontsize=9)
        self.ax_temp.set_xlabel("t [s]", fontsize=7)
        self.ax_temp.set_ylabel("T [°C]", fontsize=7)
        self.ax_temp.tick_params(labelsize=9)
        (self.line_temp,) = self.ax_temp.plot([], [], color=CLR_SERIES_ORANGE, lw=1.2, label="T_DUT (misurata)")
        (self.line_setpoint,) = self.ax_temp.plot([], [], color=INK_MUTED, lw=1.0, ls="--", alpha=0.8, label="Setpoint")
        self._style_legend(self.ax_temp.legend(loc="upper right", fontsize=9))

        self.canvas_rtu_temp = FigureCanvasTkAgg(self.fig_rtu_temp, master=body)
        self.canvas_rtu_temp.get_tk_widget().configure(bg=BG_PANEL, highlightthickness=0)
        self.canvas_rtu_temp.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.canvas_rtu_temp.draw()

        # Scorrevole: la meta' inferiore ha altezza fissa (uniform="rtu_split"
        # sopra), ma l'elenco di controlli puo' superarla (es. dopo l'aggiunta
        # di nuovi pulsanti) — senza scroll gli elementi in fondo finiscono
        # oltre il bordo della finestra e diventano irraggiungibili.
        ctrl = ctk.CTkScrollableFrame(body, fg_color="transparent")
        ctrl.grid(row=1, column=0, sticky="nsew")
        ctrl.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ctrl, text="PID CTRL + RTU", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, padx=15, pady=(6, 0))
        ctk.CTkLabel(ctrl, text="(placeholder — TBD)", font=ctk.CTkFont(size=10), text_color=INK_MUTED).grid(row=1, column=0, padx=15)

        self.lbl_rtu_status = ctk.CTkLabel(ctrl, text="● non connesso", text_color="white", fg_color=CLR_DANGER,
                                           corner_radius=6, font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_rtu_status.grid(row=2, column=0, padx=15, pady=(6, 3), sticky="ew")

        addr = ctk.CTkFrame(ctrl, fg_color="transparent")
        addr.grid(row=3, column=0, padx=15, pady=1, sticky="ew")
        ctk.CTkLabel(addr, text="IP:").grid(row=0, column=0, padx=(0, 3), sticky="e")
        ctk.CTkEntry(addr, textvariable=self.rtu_host_val, width=100).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(addr, text="Porta:").grid(row=1, column=0, padx=(0, 3), sticky="e")
        ctk.CTkEntry(addr, textvariable=self.rtu_port_val, width=60).grid(row=1, column=1, sticky="w")

        self.btn_rtu_conn = ctk.CTkButton(ctrl, text="Connetti", fg_color=CLR_ACCENT_CONFIG, hover_color=CLR_ACCENT_CONFIG_HOVER, command=self._rtu_toggle_connection)
        self.btn_rtu_conn.grid(row=4, column=0, padx=15, pady=6)

        mf = ctk.CTkFrame(ctrl, fg_color=BG_PANEL_RAISED, corner_radius=8)
        mf.grid(row=5, column=0, padx=15, pady=3, sticky="ew")
        self.metric_temp      = self._add_metric(mf, 0, "T_DUT [°C]", "—")
        self.metric_pwm       = self._add_metric(mf, 1, "PWM PID [%]", "—")
        self.metric_pid_state = self._add_metric(mf, 2, "Stato PID", "—")

        self.btn_rtu_ack = ctk.CTkButton(ctrl, text="ACK FAULT (RTU/PID)", fg_color=CLR_WARN_DEEP, hover_color=CLR_WARN_DEEP_HOVER,
                                          command=lambda: self._rtu_send_cmd("ACK FAULT"))
        self.btn_rtu_ack.grid(row=6, column=0, padx=15, pady=(2, 4), sticky="ew")

        sp = ctk.CTkFrame(ctrl, fg_color="transparent")
        sp.grid(row=7, column=0, padx=15, pady=(4, 2), sticky="ew")
        sp.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(sp, text="Setpoint (°C):").grid(row=0, column=0, columnspan=2, sticky="w")
        entry_setpoint = ctk.CTkEntry(sp, textvariable=self.setpoint_val, width=70, justify="center")
        entry_setpoint.grid(row=1, column=0, padx=(0, 4), pady=(2, 0), sticky="ew")
        entry_setpoint.bind("<Return>", lambda e: self._rtu_set_setpoint())
        ctk.CTkButton(sp, text="Set", width=60, command=self._rtu_set_setpoint).grid(row=1, column=1, pady=(2, 0), sticky="ew")

    # ---------------------------------------------------------------- Colonna CENTRO
    def _build_center(self, p):
        p.grid_columnconfigure(0, weight=1)
        r = 0

        # --- Azioni DUT ---
        sec = self._section(p, r, "Azioni DUT", kind="protect"); r += 1
        act = ctk.CTkFrame(sec, fg_color="transparent"); act.pack(fill="x")
        self.btn_dut_on = ctk.CTkButton(act, text="DUT ON", width=64, fg_color=CLR_OK, hover_color=CLR_OK_HOVER, command=lambda: self._send_cmd("DUT_ON"))
        self.btn_dut_on.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_dut_off = ctk.CTkButton(act, text="DUT OFF", width=64, fg_color=CLR_DANGER, hover_color=CLR_DANGER_HOVER, command=lambda: self._send_cmd("DUT_OFF"))
        self.btn_dut_off.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_reset = ctk.CTkButton(act, text="RESET", width=64, fg_color=CLR_WARN, hover_color=CLR_WARN_HOVER, command=lambda: self._send_cmd("RESET"))
        self.btn_reset.pack(side="left", padx=4, expand=True, fill="x")
        self.btn_ack = ctk.CTkButton(act, text="ACK FAULT", width=64, fg_color=CLR_WARN_DEEP, hover_color=CLR_WARN_DEEP_HOVER, command=lambda: self._send_cmd("ACK FAULT"))
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
        self.btn_pause = ctk.CTkButton(toolbar, text="⏸ Pausa", width=70, command=self._toggle_pause)
        self.btn_pause.pack(side="left", padx=2)

        self.btn_auto = ctk.CTkButton(toolbar, text="🔄 Auto Zoom", width=90, fg_color=CLR_ACCENT_CONFIG, hover_color=CLR_ACCENT_CONFIG_HOVER, command=self._reset_zoom)
        self.btn_auto.pack(side="left", padx=2)

        ctk.CTkButton(toolbar, text="Azzera", width=60, fg_color=CLR_NEUTRAL, hover_color=CLR_NEUTRAL_HOVER, command=self._clear_plots).pack(side="left", padx=2)

        self.fig = Figure(figsize=(3.8, 5.0), dpi=100)
        self.fig.subplots_adjust(hspace=0.35, left=0.16, right=0.95, top=0.96, bottom=0.06)
        self._style_figure(self.fig)

        self.ax_slow = self.fig.add_subplot(311)
        self._style_axes(self.ax_slow)
        self.ax_slow.set_title("Corrente DUT (log 10 Hz)", fontsize=10)
        self.ax_slow.set_xlabel("t [s]", fontsize=8)
        self.ax_slow.set_ylabel("I [mA]", fontsize=8)
        self.ax_slow.yaxis.set_major_locator(ticker.MaxNLocator(nbins=12))
        self.ax_slow.yaxis.set_minor_locator(ticker.AutoMinorLocator())
        self.ax_slow.tick_params(labelsize=10)
        (self.line_slow,) = self.ax_slow.plot([], [], color=CLR_SERIES_BLUE, lw=1.2, label="I")
        (self.line_thr,) = self.ax_slow.plot([], [], color=CLR_DANGER, lw=1.0, ls="--", alpha=0.8, label="soglia (V_LIMIT)")
        (self.line_slow_trace,) = self.ax_slow.plot([], [], color=CLR_SERIES_VIOLET, lw=1.3, label="evento (100 kSa/s)")
        self._style_legend(self.ax_slow.legend(loc="upper right", fontsize=9))

        self.ax_volt = self.fig.add_subplot(312)
        self._style_axes(self.ax_volt)
        self.ax_volt.set_title("Tensione DUT (misurata)", fontsize=10)
        self.ax_volt.set_xlabel("t [s]", fontsize=8)
        self.ax_volt.set_ylabel("V [V]", fontsize=8)
        self.ax_volt.tick_params(labelsize=10)
        (self.line_slow_v,) = self.ax_volt.plot([], [], color=CLR_SERIES_AQUA, lw=1.2, label="V")

        self.ax_trace = self.fig.add_subplot(313)
        self.ax_trace.set_navigate(False)
        self._style_axes(self.ax_trace)
        self.ax_trace.set_title("Ultima traccia evento", fontsize=10)
        self.ax_trace.set_xlabel("t [us]", fontsize=8)
        self.ax_trace.set_ylabel("I [mA]", fontsize=8)
        self.ax_trace.tick_params(labelsize=10)
        (self.line_trace,) = self.ax_trace.plot([], [], color=CLR_SERIES_VIOLET, lw=1.0)

        self.canvas = FigureCanvasTkAgg(self.fig, master=p)
        self.canvas.get_tk_widget().configure(bg=BG_PANEL, highlightthickness=0)
        self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("motion_notify_event", self._on_drag)
        
        self.canvas.draw()

    def _toggle_pause(self):
        self.plot_paused = not self.plot_paused
        self.btn_pause.configure(text="▶ Riprendi" if self.plot_paused else "⏸ Pausa",
                                 fg_color=CLR_OK if self.plot_paused else CLR_ACCENT_CONFIG)

    def _on_scroll(self, event):
        if event.inaxes not in (self.ax_slow, self.ax_volt):
            return
        
        ax = event.inaxes
        cur_xlim = ax.get_xlim()
        cur_ylim = ax.get_ylim()
        
        xdata = event.xdata
        ydata = event.ydata
        if xdata is None or ydata is None:
            return

        if ax == self.ax_slow:
            self._zoomed_slow = True
        elif ax == self.ax_volt:
            self._zoomed_volt = True
        self.btn_auto.configure(fg_color=CLR_NEUTRAL)

        base_scale = 1.2
        if event.button == 'up':
            scale_factor = 1 / base_scale
        elif event.button == 'down':
            scale_factor = base_scale
        else:
            scale_factor = 1

        new_width = (cur_xlim[1] - cur_xlim[0]) * scale_factor
        new_height = (cur_ylim[1] - cur_ylim[0]) * scale_factor

        relx = (cur_xlim[1] - xdata) / (cur_xlim[1] - cur_xlim[0])
        rely = (cur_ylim[1] - ydata) / (cur_ylim[1] - cur_ylim[0])

        ax.set_xlim([xdata - new_width * (1 - relx), xdata + new_width * relx])
        ax.set_ylim([ydata - new_height * (1 - rely), ydata + new_height * rely])
        self.canvas.draw_idle()

    def _on_press(self, event):
        if event.inaxes not in (self.ax_slow, self.ax_volt):
            return
        if event.button == 1:
            self._pan_start = (event.x, event.y)
            self._pan_axes = event.inaxes
            self._pan_xlim = event.inaxes.get_xlim()
            self._pan_ylim = event.inaxes.get_ylim()

    def _on_drag(self, event):
        if not getattr(self, '_pan_start', None) or not getattr(self, '_pan_axes', None):
            return
            
        ax = self._pan_axes
        if ax == self.ax_slow:
            self._zoomed_slow = True
        elif ax == self.ax_volt:
            self._zoomed_volt = True
        self.btn_auto.configure(fg_color=CLR_NEUTRAL)
        x0, y0 = ax.transData.inverted().transform(self._pan_start)
        x1, y1 = ax.transData.inverted().transform((event.x, event.y))
        
        dx = x1 - x0
        dy = y1 - y0

        ax.set_xlim([self._pan_xlim[0] - dx, self._pan_xlim[1] - dx])
        ax.set_ylim([self._pan_ylim[0] - dy, self._pan_ylim[1] - dy])
        self.canvas.draw_idle()

    def _on_release(self, event):
        self._pan_start = None
        self._pan_axes = None

    def _reset_zoom(self):
        self._zoomed_slow = False
        self._zoomed_volt = False
        self.btn_auto.configure(fg_color=CLR_ACCENT_CONFIG)
        self.ax_slow.autoscale(enable=True, axis='both')
        self.ax_volt.autoscale(enable=True, axis='both')
        self._plot_dirty = True

    def _clear_plots(self):
        self._reset_zoom()
        self.slow_t.clear(); self.slow_i.clear(); self.slow_v.clear(); self.slow_thr.clear()
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
            self.line_slow_v.set_data([], [])
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

    def _counts_to_mA(self, adc_raw):
        try:
            r = float(self.r_shunt.get()); g = float(self.ina_gain.get())
        except ValueError:
            return None
        return counts_to_mA(adc_raw, r, g)

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
                    self.line_slow_v.set_data(list(self.slow_t), list(self.slow_v))
                    
                    if not getattr(self, '_zoomed_slow', False):
                        self.ax_slow.relim(); self.ax_slow.autoscale_view()
                    if not getattr(self, '_zoomed_volt', False):
                        self.ax_volt.relim(); self.ax_volt.autoscale_view()
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
                                        fg_color=BG_PANEL_RAISED, corner_radius=8)
        self.slow_log.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="nsew")
        self.log.tag_config("tx", foreground=CLR_ACCENT_CONFIG)
        self.log.tag_config("rx", foreground=CLR_OK)
        self.log.tag_config("info", foreground=CLR_WARN)
        self.log.tag_config("err", foreground=CLR_DANGER)
        self.slow_log.tag_config("trace", foreground=CLR_SERIES_VIOLET)

    # ============================================================ CONNESSIONE
    def _toggle_connection(self):
        if self.client.connected:
            self._disconnect()
        else:
            threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        self._log(f"Connessione a {self.client.host}:{self.client.port}...", "info")
        if self.client.connect():
            self.after(0, self._on_connected)

    def _disconnect(self):
        self.ping_loop_active = False
        self.client.disconnect()
        self._on_disconnected()

    def _on_connected(self):
        self.lbl_status.configure(text="● CONNESSO", fg_color=CLR_OK)
        self.btn_conn.configure(text="Disconnetti", fg_color=CLR_DANGER, hover_color=CLR_DANGER_HOVER)
        self._log("Connesso.", "info")
        try:
            prefix = self.client.open_run_files(self._run_prefix())
            self._log(f"File di run: {prefix}_*.csv", "info")
            self.client.log_event("CONNECT", f"{self.client.host}:{self.client.port}")
            self.slow_t.clear(); self.slow_i.clear(); self.slow_v.clear(); self.slow_thr.clear(); self.slow_t0 = None
            # Protocollo v5: invia la config elettrica/parametrica al firmware
            self._send_config()
        except Exception as e:
            self._log(f"Errore apertura file di run: {e}", "err")

    def _on_disconnected(self):
        self.client.log_event("DISCONNECT")
        self.client.close_run_files()
        self.lbl_status.configure(text="● DISCONNESSO", fg_color=CLR_DANGER)
        self.btn_conn.configure(text="Connetti", fg_color=CLR_ACCENT_CONFIG, hover_color=CLR_ACCENT_CONFIG_HOVER)
        self.btn_ping_loop.configure(fg_color="transparent")
        self._log("Disconnesso.", "info")

    # ============================================================ RTU/PID (Figura 1, placeholder)
    def _rtu_toggle_connection(self):
        if self.rtu_client.connected:
            self._rtu_disconnect()
        else:
            threading.Thread(target=self._rtu_connect, daemon=True).start()

    def _rtu_connect(self):
        try:
            host = self.rtu_host_val.get().strip()
            port = int(self.rtu_port_val.get())
        except ValueError:
            self.rtu_client.rx_queue.put(("err", "RTU/PID: IP o porta non validi."))
            return
        self._log(f"RTU/PID: connessione a {host}:{port}...", "info")
        if self.rtu_client.connect(host, port):
            self.after(0, self._rtu_on_connected)

    def _rtu_disconnect(self):
        self.rtu_client.disconnect()
        self._rtu_on_disconnected()

    def _rtu_on_connected(self):
        self.lbl_rtu_status.configure(text="● connesso", fg_color=CLR_OK)
        self.btn_rtu_conn.configure(text="Disconnetti RTU/PID", fg_color=CLR_DANGER, hover_color=CLR_DANGER_HOVER)
        self.rtu_t.clear(); self.rtu_temp.clear(); self.rtu_sp.clear()
        self.rtu_t0 = None
        self._log("RTU/PID: connesso.", "info")

    def _rtu_on_disconnected(self):
        self.lbl_rtu_status.configure(text="● non connesso", fg_color=CLR_DANGER)
        self.btn_rtu_conn.configure(text="Connetti RTU/PID", fg_color=CLR_ACCENT_CONFIG, hover_color=CLR_ACCENT_CONFIG_HOVER)
        self.metric_temp.configure(text="—")
        self.metric_pwm.configure(text="—")
        self.metric_pid_state.configure(text="—")
        self._log("RTU/PID: disconnesso.", "info")

    def _rtu_send_cmd(self, cmd):
        if self.rtu_client.send_cmd(cmd):
            self._log(f"RTU/PID -> {cmd}", "tx")

    def _rtu_set_setpoint(self):
        raw = self.setpoint_val.get().strip().replace(",", ".")
        try:
            sp = float(raw)
        except ValueError:
            self._log(f"RTU/PID: setpoint '{self.setpoint_val.get()}' non valido (usare un numero, es. 85.0).", "err")
            return
        self._rtu_send_cmd(f"SET SETPOINT_C {sp:.1f}")

    def _poll_rtu_queue(self):
        try:
            while True:
                kind, msg = self.rtu_client.rx_queue.get_nowait()
                if kind == "rx":
                    fields = parse_kv(msg)
                    if "TEMP" in fields:
                        try:
                            temp = float(fields["TEMP"].replace(",", "."))
                            self.metric_temp.configure(text=f"{temp:.1f}")
                            now = time.time()
                            if self.rtu_t0 is None:
                                self.rtu_t0 = now
                            try:
                                sp = float(self.setpoint_val.get().replace(",", "."))
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
                            self.metric_pwm.configure(text=f"{float(fields['PWM'].replace(',', '.')):.1f}")
                        except ValueError:
                            pass
                    if "STATE" in fields:
                        self.metric_pid_state.configure(text=fields["STATE"])
                    self._log(f"RTU/PID <- {msg}", "rx")
                elif kind == "err":
                    self._log(msg, "err")
                elif kind == "disconnected":
                    self._log(msg, "err")
                    self._rtu_on_disconnected()
        except queue.Empty:
            pass
        self.after(40, self._poll_rtu_queue)

    def _on_close(self):
        if self.client.connected:
            self._disconnect()
        if self.rtu_client.connected:
            self._rtu_disconnect()
        self.destroy()

    # ================================================================== I/O
    def _send_cmd(self, cmd):
        if not self.client.connected:
            return
        if self.permanent_off and cmd.strip().upper().startswith(("DUT_ON", "SWITCH ON")):
            if not messagebox.askyesno(
                    "DUT in FAULT",
                    "Il DUT e' in FAULT.\n\n"
                    "Il firmware rifiutera' l'accensione: usare ACK FAULT o RESET.\n"
                    "Inviare comunque?"):
                self._log("Accensione annullata (DUT in FAULT).", "info")
                return
        if not self.client.send_cmd(cmd):
            return
        self.after(0, lambda: self.metric_tx.configure(text=str(self.client.tx_count)))
        if not cmd.startswith("DAC_SET"):
            self._log(f"-> {cmd}", "tx")
        if cmd.startswith(("DUT_ON", "DUT_OFF", "RESET", "TH_SELECT",
                           "TH_LOAD", "THOLD_SET", "TON_SET",
                           "INA_RST", "RETRY_SET", "TCLEAR_SET")):
            parts = cmd.split(None, 1)
            self.client.log_event(parts[0], parts[1] if len(parts) > 1 else "")

    def _send_manual(self):
        cmd = self.entry_cmd.get().strip()
        if cmd:
            self._send_cmd(cmd)
            self.entry_cmd.delete(0, tk.END)

    def _poll_rx_queue(self):
        try:
            while True:
                kind, msg = self.client.rx_queue.get_nowait()
                if kind == "rx":
                    if msg.startswith("LOG_10HZ"):
                        fields = parse_kv(msg)
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
                                    i_mA = self._counts_to_mA(adc_raw)
                                    if i_mA is None:
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
                                    self.slow_v.append(i_mA * self._rg_factor())
                                    self.slow_t.append(now - self.slow_t0)
                                    self.slow_thr.append(thr)
                                    self._plot_dirty = True
                                except ValueError:
                                    pass
                                pc_ts = time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int((now % 1) * 1000):03d}"
                                self.client.write_log_row(
                                    pc_ts, fields.get("TICK", ""), adc_raw, i_mA,
                                    fields.get("FRESH", ""), fields.get("STATE", ""),
                                    fields.get("RETRY", ""), fields.get("SEL", ""), fields.get("HCE", ""))
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
                        fields = parse_kv(msg)
                        try:
                            self.trace_fs = int(fields.get("FS", DEFAULT_FS))
                        except Exception:
                            self.trace_fs = DEFAULT_FS
                        self._log_slow(f"--- {msg} ---", "trace")
                        try:
                            fname = self.client.open_trace_file(
                                self._run_prefix(), self.trace_lbl, msg,
                                self.r_shunt.get(), self.ina_gain.get())
                            self.client.log_event("TRACE", f"{self.trace_lbl} {fname}")
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
                        self.client.close_trace_file()
                        continue
                    elif self.trace_active:
                        try:
                            parts = msg.split(",")
                            if len(parts) == 2:
                                idx = int(parts[0]); adc_raw = int(parts[1])
                                time_us = idx * 1e6 / self.trace_fs
                                i_mA = self._counts_to_mA(adc_raw)
                                if i_mA is None:
                                    i_mA = ""
                                try:
                                    self._trace_acc.append((time_us, float(i_mA)))
                                    if self.trace_overlay_t0 is not None:
                                        self._trace_overlay_acc.append(
                                            (self.trace_overlay_t0 + time_us / 1e6, float(i_mA))
                                        )
                                except ValueError:
                                    pass
                                self.client.write_trace_row(time_us, adc_raw, i_mA)
                                self.trace_sample_idx += 1
                            else:
                                self.client.write_trace_comment(msg)
                        except Exception:
                            pass
                        continue

                    if msg == "PONG" or msg.startswith("PONG"):
                        if self.client.last_rtt is not None:
                            self.client.ping_sent_time = None
                            rtt = self.client.last_rtt
                            self.metric_ping.configure(text=f"{rtt:.1f} ms")
                            self._log(f"<- {msg}  [{rtt:.1f} ms]", "rx")
                            self.client.last_rtt = None
                        else:
                            self._log(f"<- {msg}", "rx")
                        self.client.rx_count += 1
                        self.metric_rx.configure(text=str(self.client.rx_count))
                        continue

                    self.client.rx_count += 1
                    self.metric_rx.configure(text=str(self.client.rx_count))

                    if msg.startswith("OK STATUS="):
                        f = parse_kv(msg)
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
                            f2 = parse_kv(msg)
                            self.cur_dac = int(f2.get("DAC", self.cur_dac))
                            self.metric_dac.configure(text=str(self.cur_dac))
                            self.metric_dacv.configure(text=f"{self.cur_dac / DAC_MAX_COUNTS * VREF:.2f} V")
                        except Exception:
                            pass

                    if not msg.startswith("DAC_SET"):
                        self._log(f"<- {msg}", "rx")
                elif kind == "err":
                    self._log(msg, "err")
                elif kind == "disconnected":
                    self._log(msg, "err")
                    self._on_disconnected()
        except queue.Empty:
            pass
        self.after(40, self._poll_rx_queue)

    # ============================================================ HELPERS
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
            color = INK_SECONDARY
        self.metric_state.configure(text=name, text_color=color)
        if retry is not None:
            try:
                nmax = int(self.retry_max.get())
            except (ValueError, AttributeError):
                nmax = SEL_RETRY_MAX
            self.metric_retries.configure(text=f"{retry}/{nmax}",
                                          text_color=CLR_DANGER if retry >= nmax else INK_SECONDARY)
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

    def _run_prefix(self):
        def clean(s):
            s = s.strip().replace(" ", "-")
            s = "".join(ch for ch in s if ch.isalnum() or ch in "-_.")
            return s or "NA"
        return f"{clean(self.dut_id.get())}_{clean(self.let_id.get())}_{clean(self.run_id.get())}"

    # ============================================================ PING LOOP
    def _toggle_ping_loop(self):
        if self.ping_loop_active:
            self.ping_loop_active = False
            self.btn_ping_loop.configure(fg_color="transparent")
        else:
            self.ping_loop_active = True
            self.btn_ping_loop.configure(fg_color=CLR_WARN)
            threading.Thread(target=self._ping_loop_thread, daemon=True).start()

    def _ping_loop_thread(self):
        while self.ping_loop_active and self.client.connected:
            try:
                self._send_cmd("PING")
            except Exception as e:
                self.client.rx_queue.put(("err", f"Ping loop error: {e}"))
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
