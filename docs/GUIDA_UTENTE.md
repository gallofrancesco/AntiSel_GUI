# Guida Utente — AntiSEL Dashboard

Guida pratica all'uso della dashboard `antisel_dashboard_eth.py`: applicazione
desktop (CustomTkinter) per il controllo e il monitoraggio via Ethernet TCP/IP
del sistema di protezione AntiSEL su NUCLEO-H755ZI-Q.

Per il contratto di comunicazione firmware ↔ GUI vedi
[AntiSEL_Protocollo_Comandi.md](AntiSEL_Protocollo_Comandi.md). Per la
descrizione funzionale del sistema vedi
[AntiSEL_System_Description.pdf](AntiSEL_System_Description.pdf) e
[AntiSEL_Flow_Description_G.docx](AntiSEL_Flow_Description_G.docx).

## 1. Installazione

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requisiti: Python 3.7+ (Tkinter incluso nella stdlib), `customtkinter`,
`matplotlib`, `numpy`.

## 2. Configurazione di rete

L'indirizzo del dispositivo è definito in cima a `antisel_dashboard_eth.py`:

```python
HOST    = "192.168.1.100"   # IP della NUCLEO-H755ZI-Q
PORT    = 7755              # porta TCP del server firmware
TIMEOUT = 3.0                # timeout socket [s]
```

Modificare questi valori (e rilanciare l'app) se il dispositivo ha un IP o
una porta diversi.

## 3. Avvio

```bash
python antisel_dashboard_eth.py
```

## 4. Layout dell'interfaccia

La finestra è divisa in quattro colonne più un'area di log in basso:

| Colonna | Contenuto |
|---|---|
| **Sinistra** | Stato connessione Nucleo, pulsante Connetti/Disconnetti, metriche RTT/TX/RX, test di rete (PING, STATUS, Ping Loop), campo comando libero |
| **Centro** | Azioni DUT, gestione riarmo latch INA301, soglia `I_TH`, tempistiche `T_HOLD`/`T_ON`, parametri hardware (`R_SHUNT`, `GAIN`), identificativi run (per i nomi dei CSV), pannello di stato |
| **Grafici** | Grafico continuo (log 10 Hz) e grafico dell'ultima traccia evento ad alta risoluzione |
| **Destra** | Pannello **PID CTRL / RTU** — placeholder per il controllo temperatura (Figura 1 della descrizione di sistema): grafico temperatura in cima (§4.11), connessione TCP separata, PWM/stato PID, setpoint (vedi §4.10) |
| **In basso** | Log cronologico colorato di tutte le operazioni |

### 4.1 Connessione

1. Verificare/impostare `HOST`/`PORT` (vedi §2).
2. Cliccare **Connetti**. Lo stato passa a `● CONNESSO` (verde); un
   `DISCONNESSO` rosso indica che il socket non è (ancora) collegato.
3. **Disconnetti** chiude la connessione TCP in modo pulito.

La ricezione dati avviene su un thread separato per non bloccare la UI; i
messaggi vengono accodati e processati dal loop principale ogni ~40 ms.

### 4.2 Test di rete

- **PING** → invia `PING`, atteso `PONG`; l'RTT misurato appare nel pannello
  metriche.
- **STATUS** → invia `STATUS`/`GET STATUS`, risposta con stato macchina,
  allarme, switch, override, retry, contatori SEL/HCE.
- **Ping Loop** → invia `PING` automaticamente ogni secondo, utile per
  monitorare la stabilità del collegamento.
- **Campo comando libero** → invia un comando testuale arbitrario (utile in
  debug o per comandi non ancora esposti da un pulsante).

### 4.3 Azioni DUT

- **DUT ON** / **DUT OFF** → equivalgono a `SWITCH ON` / `SWITCH OFF`
  (override manuale del DUT).
- **RESET** → azzera contatori e riarma la protezione.
- **ACK FAULT** → richiesto per uscire dallo stato `FAULT`.

Se il sistema è in stato di spegnimento permanente (`permanent_off`), la GUI
avvisa che il firmware rifiuterà l'accensione finché non si esegue `ACK
FAULT` o `RESET`.

### 4.4 Latch INA301 (policy di riarmo)

- **Reset allarme** → invia `INA_RST` (impulso diagnostico, no-op sicuro in
  transparent mode).
- **N (RETRY_MAX)** → numero massimo di riarmi consecutivi prima del blocco
  permanente; **Set** invia `SET RETRY_MAX <n>`.
- **T_CLEAR** → finestra (ms) di corrente "pulita" richiesta per considerare
  chiuso un evento; **Set** invia `SET TCLEAR_MS <ms>`.

### 4.5 Soglia di corrente I_TH

Campo numerico (mA, range 1.0–50.0 come da requisito R‑02) con pulsanti
±0.1/±1 per regolazioni fini, e **Set** per applicare (`SET THRESHOLD_MA
<mA>`, invio Return equivalente). La GUI mostra anche il valore DAC stimato
corrispondente.

### 4.6 Tempistiche T_HOLD / T_ON

Campi numerici in millisecondi; l'invio moltiplica per 1000 e manda
`SET THOLD_US <us>` / `SET TON_US <us>` (range 1–10 ms, risoluzione ≤100 µs
come da requisiti R‑03/R‑04).

### 4.7 Parametri hardware

`R_SHUNT` [Ω] e `GAIN` INA301 (20/50/100 V/V): usati per calcolare
localmente il DAC atteso e per la conversione ADC→mA nei grafici; vengono
anche inviati al firmware (`SET RSHUNT`, `SET GAIN`) tramite l'invio
configurazione complessivo.

### 4.8 Identificativi Run (nomi file CSV)

`DUT id`, `LET`, `Run id` compongono il prefisso dei file generati durante
una sessione di log/trace (vedi §5). Impostarli prima di avviare
l'acquisizione per ottenere nomi file riconoscibili.

### 4.9 Grafici

- **Grafico continuo**: traccia `I_MA`/soglia nel tempo a partire dai
  messaggi `LOG_10HZ`; supporto pausa/ripresa e pulizia.
- **Grafico traccia evento**: mostra l'ultima traccia ad alta risoluzione
  (`TRACE_START`…`TRACE_END`) con overlay sul grafico continuo nel punto
  temporale corretto.

### 4.10 Pannello PID CTRL / RTU (placeholder)

La descrizione di sistema (Figura 1) prevede, oltre alla AntiSEL Board, un
**PID CTRL** che pilota il PWM dell'Heat System della Irradiation Board e un
**RTU** che legge la temperatura del DUT e la fornisce a PID e PC. Questi due
dispositivi non sono ancora disponibili in laboratorio (IP, porta e
protocollo sono un punto aperto, §8.4 della descrizione di sistema): il
pannello nella colonna destra è quindi un **placeholder**, pronto per essere
agganciato quando l'hardware sarà definito.

- **IP / Porta**: indirizzo TCP del link RTU/PID, indipendente da quello
  della Nucleo (default `192.168.1.101:7756`, costanti `RTU_HOST`/`RTU_PORT`
  in cima al file).
- **Connetti/Disconnetti**: apre/chiude una connessione TCP separata da
  quella della Nucleo (thread RX dedicato, non interferisce col resto della
  dashboard).
- **T_DUT / PWM PID / Stato PID**: aggiornati automaticamente una volta al
  secondo tramite un polling `GET TEMP` / `GET PID` (protocollo testuale
  provvisorio, in stile `GET`/`SET`/`OK` coerente con quello della Nucleo).
- **Setpoint**: campo in °C (default 85.0, in linea con `T_DUT` richiesto da
  AD2 §3); **Set setpoint** invia `SET SETPOINT_C <valore>`.

Quando le specifiche reali di PID CTRL e RTU saranno disponibili (protocollo
testuale, Modbus TCP o altro), è sufficiente aggiornare il parsing in
`_rtu_send_cmd`/`_poll_rtu_queue` senza modificare il resto della dashboard.

La logica di controllo raccomandata per il PID CTRL reale (anti-windup,
frequenza di aggiornamento, guadagni) è descritta in
[AntiSEL_Protocollo_Comandi.md](AntiSEL_Protocollo_Comandi.md#logica-di-controllo-raccomandata-per-il-pid-ctrl),
con implementazione di riferimento in [`pid_controller.py`](../pid_controller.py).

### 4.11 Grafico temperatura

In cima alla quarta colonna (PID CTRL/RTU), allineato con il grafico
"Corrente DUT" della colonna grafici, compare un grafico dedicato con
`T_DUT` (dall'RTU) nel tempo e una linea tratteggiata di riferimento al
setpoint corrente. Si aggiorna automaticamente a ogni lettura `GET TEMP` e si
azzera insieme agli altri grafici con il pulsante **Azzera** (colonna
grafici), o alla riconnessione del link RTU/PID.

## 5. File generati (CSV)

Durante una sessione con logging attivo, la dashboard scrive nella
directory corrente:

- `<DUTid>_<LET>_<Runid>_log10hz.csv` — log continuo a 10 Hz
  (`PC_Time,Tick_ms,ADC_raw,Current_mA,Fresh,State,Retry,SEL,HCE`)
- `<DUTid>_<LET>_<Runid>_events.csv` — eventi testuali (`PC_Time,Event,Detail`)
- `<DUTid>_<LET>_<Runid>_<timestamp>_trace_<SEL|HCE>.csv` — traccia ad alta
  risoluzione di ogni evento SEL/HCE (`Time_us,ADC_raw,Current_mA`)

## 6. Log colorato

- **Blu**: comandi trasmessi (TX)
- **Verde**: dati ricevuti (RX)
- **Giallo**: messaggi informativi
- **Rosso**: errori

## 7. Riferimento comandi (sintesi)

| Comando | Effetto |
|---|---|
| `PING` | Test di connettività, risposta `PONG` |
| `GET STATUS` / `STATUS` | Stato corrente del sistema |
| `GET CONFIG` | Configurazione runtime completa |
| `SET THRESHOLD_MA <mA>` | Imposta soglia I_TH |
| `SET THOLD_US <us>` / `SET TON_US <us>` | Imposta tempistiche |
| `SET RETRY_MAX <n>` / `SET TCLEAR_MS <ms>` | Policy di riarmo |
| `SET GAIN <v>` / `SET RSHUNT <ohm>` | Parametri elettrici |
| `SWITCH ON` / `SWITCH OFF` (alias `DUT_ON`/`DUT_OFF`) | Accensione/spegnimento DUT |
| `RESET` | Reset contatori e riarmo protezione |
| `ACK FAULT` | Uscita dallo stato FAULT |
| `INA_RST` | Reset diagnostico allarme INA301 |

Sintassi completa, range di validazione e formati di telemetria asincrona
(`LOG_10HZ`, `TRACE_START`/`TRACE_END`) sono documentati in
[AntiSEL_Protocollo_Comandi.md](AntiSEL_Protocollo_Comandi.md).

## 8. Risoluzione problemi

1. Verificare `HOST`/`PORT` in `antisel_dashboard_eth.py`.
2. Verificare la raggiungibilità del dispositivo (`ping <HOST>` da terminale).
3. Controllare il log applicazione (area in basso, righe rosse = errori).
4. Se il DUT risulta bloccato in `FAULT`/spegnimento permanente, usare
   **ACK FAULT** o **RESET**.
5. Il pannello **PID CTRL / RTU** è un placeholder (§4.10): se non compaiono
   letture non è un malfunzionamento, ma la semplice assenza del dispositivo
   reale (protocollo/IP ancora da definire).
