"""
Backend AntiSEL Dashboard — comunicazione TCP con NUCLEO-H755ZI-Q e con il
link RTU/PID (Figura 1, rif. RD04, placeholder — vedi costanti RTU_*),
protocollo testuale GET/SET/OK, logging su CSV (run + traccia evento) e
conversioni elettriche (mA <-> DAC/ADC counts).

Nessuna dipendenza da tkinter/customtkinter: la GUI consuma questo modulo
tramite le rx_queue dei client (coppie (kind, msg) con kind in
"rx"/"err"/"disconnected") e ne invoca i metodi.
"""

import socket
import threading
import time
import queue

HOST    = "192.168.1.100"
PORT    = 7755
TIMEOUT = 3.0

# IP/porta/protocollo TBD (§8.4): placeholder in stile testuale GET/SET/OK,
# coerente con quello della Nucleo, pensato per essere sostituito quando le
# specifiche reali (o Modbus TCP) saranno definite.
RTU_HOST    = "192.168.1.101"
RTU_PORT    = 7756
RTU_TIMEOUT = 10.0
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


def voltage_to_counts(v, vref=VREF):
    return int(max(0, min(DAC_MAX_COUNTS, round(v / vref * DAC_MAX_COUNTS))))


def counts_to_mA(adc_raw, r_shunt, gain):
    """None se r_shunt/gain non validi (es. 0), altrimenti corrente in mA."""
    try:
        v_adc = (adc_raw / ADC_MAX_COUNTS) * VREF
        return (v_adc / (r_shunt * gain)) * 1000.0
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def parse_kv(msg):
    out = {}
    for tok in msg.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


class NucleoClient:
    """Socket TCP verso il firmware Nucleo: connessione, RX a linee, invio
    comandi, logging su CSV (log 10 Hz, eventi, traccia evento ad alta
    risoluzione)."""

    def __init__(self, host=HOST, port=PORT, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.connected = False
        self.rx_queue = queue.Queue()
        self.rx_thread = None
        self.tx_count = 0
        self.rx_count = 0
        self.ping_sent_time = None
        self.last_rtt = None   # RTT misurato all'arrivo del PONG [ms]
        self.log_csv = None
        self.events_csv = None
        self.trace_file = None

    # ---------------------------------------------------------------- connessione
    def connect(self):
        """Bloccante: chiamare in un thread dedicato. Ritorna True se connesso."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            
            # Abilita TCP Keep-Alive aggressivo (rileva disconnessione cavo in ~4 sec)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                s.setsockopt(socket.IPPROTO_TCP, 18, 4000) # TCP_USER_TIMEOUT = 4 sec
            except AttributeError:
                pass
            try:
                s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 1000, 1000))
            except (AttributeError, OSError):
                pass

            # s.settimeout(None)  <-- Rimosso per permettere a recv() di lanciare socket.timeout
            self.sock = s
            self.connected = True
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()
            return True
        except Exception as e:
            self.rx_queue.put(("err", f"Connessione fallita: {e}"))
            return False

    def disconnect(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except Exception:
                pass
            self.sock = None

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
                            self.last_rtt = (time.time() - self.ping_sent_time) * 1000.0
                        self.rx_queue.put(("rx", line))
        except Exception:
            pass
        finally:
            if self.connected:
                self.connected = False
                self.rx_queue.put(("disconnected", "Connessione persa."))

    def send_cmd(self, cmd):
        """Invia un comando raw; ritorna True se inviato. Conferme utente e
        logging specifici del comando restano a carico del chiamante."""
        if not self.connected or not self.sock:
            return False
        try:
            if cmd == "PING":
                self.ping_sent_time = time.time()
            self.sock.sendall((cmd + "\r\n").encode())
            self.tx_count += 1
            return True
        except Exception as e:
            self.rx_queue.put(("err", f"Errore TX: {e}"))
            return False

    # ---------------------------------------------------------------- CSV run
    def open_run_files(self, prefix):
        """Apre log10hz.csv + events.csv con timestamp; ritorna il prefisso
        completo (per messaggi di log)."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        full_prefix = f"{prefix}_{ts}"
        self.log_csv = open(f"{full_prefix}_log10hz.csv", "w")
        self.log_csv.write("PC_Time,Tick_ms,ADC_raw,Current_mA,Fresh,State,Retry,SEL,HCE\n")
        self.events_csv = open(f"{full_prefix}_events.csv", "w")
        self.events_csv.write("PC_Time,Event,Detail\n")
        return full_prefix

    def close_run_files(self):
        if self.log_csv:
            try: self.log_csv.close()
            except Exception: pass
            self.log_csv = None
        if self.events_csv:
            try: self.events_csv.close()
            except Exception: pass
            self.events_csv = None

    def write_log_row(self, pc_ts, tick, adc_raw, i_mA, fresh, state, retry, sel, hce):
        if self.log_csv:
            self.log_csv.write(f"{pc_ts},{tick},{adc_raw},{i_mA},{fresh},{state},{retry},{sel},{hce}\n")

    def log_event(self, event, detail=""):
        if self.events_csv:
            try:
                pc_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.events_csv.write(f"{pc_ts},{event},{detail}\n")
                self.events_csv.flush()
            except Exception:
                pass

    # ---------------------------------------------------------------- CSV traccia evento
    def open_trace_file(self, run_prefix, event_label, header_msg, r_shunt, gain):
        """Ritorna il nome file creato."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"{run_prefix}_{ts}_trace_{event_label}.csv"
        self.trace_file = open(fname, "w")
        self.trace_file.write(f"# {header_msg}\n")
        self.trace_file.write(f"# PC_Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        self.trace_file.write(f"# R_SHUNT_ohm: {r_shunt}  INA_GAIN: {gain}\n")
        self.trace_file.write("Time_us,ADC_raw,Current_mA\n")
        return fname

    def write_trace_row(self, time_us, adc_raw, i_mA):
        if self.trace_file:
            self.trace_file.write(f"{time_us:.1f},{adc_raw},{i_mA}\n")

    def write_trace_comment(self, msg):
        if self.trace_file:
            self.trace_file.write(f"# {msg}\n")

    def close_trace_file(self):
        if self.trace_file:
            try: self.trace_file.close()
            except Exception: pass
            self.trace_file = None


class RtuClient:
    """Socket TCP verso il link RTU/PID (Figura 1, placeholder — vedi
    costanti RTU_*), con polling periodico di temperatura/stato PID."""

    def __init__(self, host=RTU_HOST, port=RTU_PORT, timeout=RTU_TIMEOUT, poll_s=RTU_POLL_S):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.poll_s = poll_s
        self.sock = None
        self.connected = False
        self.rx_queue = queue.Queue()
        self.rx_thread = None
        self.poll_active = False

    def connect(self, host=None, port=None):
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            
            # Abilita TCP Keep-Alive aggressivo (rileva disconnessione cavo in ~4 sec)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                s.setsockopt(socket.IPPROTO_TCP, 18, 4000) # TCP_USER_TIMEOUT = 4 sec
            except AttributeError:
                pass
            try:
                s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 1000, 1000))
            except (AttributeError, OSError):
                pass

            # s.settimeout(None)  <-- Rimosso per permettere a recv() di lanciare socket.timeout
            self.sock = s
            self.connected = True
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()
            self.poll_active = True
            threading.Thread(target=self._poll_loop, daemon=True).start()
            return True
        except Exception as e:
            self.rx_queue.put(("err", f"RTU/PID: connessione fallita: {e}"))
            return False

    def disconnect(self):
        self.poll_active = False
        self.connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except Exception:
                pass
            self.sock = None

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
                self.connected = False
                self.rx_queue.put(("disconnected", "RTU/PID: connessione persa."))

    def send_cmd(self, cmd):
        if not self.connected or not self.sock:
            return False
        try:
            self.sock.sendall((cmd + "\r\n").encode())
            return True
        except Exception as e:
            self.rx_queue.put(("err", f"RTU/PID errore TX: {e}"))
            return False

    def _poll_loop(self):
        """Interrogazione periodica di temperatura e stato PID (placeholder)."""
        while self.poll_active and self.connected:
            self.send_cmd("GET TEMP")
            self.send_cmd("GET PID")
            time.sleep(self.poll_s)
