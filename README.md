# AntiSEL Dashboard v4.0

Applicazione grafica per il controllo e il monitoraggio di dispositivi **AntiSEL** via comunicazione Ethernet TCP/IP.

## 📋 Descrizione

Dashboard interattiva basata su **CustomTkinter** che consente di:
- Comunicare con dispositivo NUCLEO-H755ZI-Q via TCP socket
- Monitorare lo stato della connessione in tempo reale
- Inviare comandi predefiniti e personalizzati
- Visualizzare metriche di comunicazione (RTT, pacchetti TX/RX, errori)
- Eseguire loop automatico di ping con misurazione latenza
- Mantenere log cronologico di tutte le operazioni

## 🖥️ Requisiti

- **Python** 3.7+
- **Tkinter** (incluso in Python standard)
- **CustomTkinter** (modulo esterno per UI moderna)
- **Socket TCP/IP** (modulo standard)

## ⚙️ Configurazione

Modifica i parametri di connessione nel file `antisel_dashboard_eth.py`:

```python
HOST = "192.168.1.100"    # Indirizzo IP del dispositivo
PORT = 7755               # Porta TCP
TIMEOUT = 3.0             # Timeout connessione in secondi
```

## 🚀 Avvio

```bash
python antisel_dashboard_eth.py
```

## 📡 Comandi Supportati

| Comando | Descrizione |
|---------|-------------|
| `PING` | Test di connettività (attesa PONG) |
| `STATUS` | Richiesta stato dispositivo |
| `DUT_ON` | Accensione DUT (Device Under Test) |
| `DUT_OFF` | Spegnimento DUT |
| `RESET` | Reset dispositivo |
| Comando manuale | Invio di comandi personalizzati via text entry |

## 🎨 Interfaccia Utente

- **Barra di stato**: Indicatore connessione (CONNESSO/DISCONNESSO) e indirizzo target
- **Pannello metriche**: Visualizzazione real-time di RTT, pacchetti trasmessi/ricevuti e errori
- **Log comunicazione**: Cronologia completa di TX/RX con color-coding (inclusa area dedicata per Log 10Hz e Tracce)
- **Pannello comandi**: Pulsanti azioni rapide + entry per comandi personalizzati
- **Toggle Ping Loop**: Esecuzione automatica di PING ogni secondo con misurazione latenza
- **Tab Gestione AntiSEL**: Configurazione parametri hardware (R_SHUNT, Gain) e soglie (I_TH, T_HOLD, T_ON)
- **Tab Generatore d'Onda**: Generatore di segnali per il DAC (Sinusoidale, Quadra, Triangolare) configurabile per simulazioni e test.

## 🎯 Funzionalità Principali

### Connessione TCP
- Threading separato per non bloccare l'interfaccia
- Timeout configurabile
- Reconnessione controllata

### Ricezione dati
- Buffer circolare per accumulo dati incompleti
- Parsing linea-per-linea
- Queue thread-safe per sincronizzazione

### Misurazione RTT
- Calcolo automatico round-trip time per PING
- Visualizzazione in millisecondi
- Supporto loop automatico

### Logging cromatico
- **BLU**: Comandi trasmessi (TX)
- **VERDE**: Dati ricevuti (RX)
- **GIALLO**: Messaggi informativi
- **ROSSO**: Errori

## 📝 Log Output

Formato log:
```
→ PING           # TX
← PONG [2.3 ms]  # RX con RTT
Errori TX: ...   # Errore
Timeout          # Timeout connessione
```

## 🛡️ Requisiti Sistema AntiSEL (INA301 Latch Mode)

Il sistema implementa una logica di monitoraggio e protezione hardware/software definita dai seguenti requisiti funzionali:

*   **R-01**: Monitoraggio e protezione della corrente di alimentazione del DUT (Device Under Test) mediante un sistema di rilevamento a soglia basato su INA301 in modalità Latch.
*   **R-02**: Soglia di intervento (`I_TH`) regolabile in funzione delle esigenze del componente, in un range nominale di 1 mA – 50 mA.
*   **R-03**: Tempo `T_HOLD` (intervallo fra il superamento della soglia e l'apertura dello switch) selezionabile fra 1 ms e 10 ms.
*   **R-04**: Tempo `T_ON` (intervallo fra l'apertura e la richiusura dello switch) selezionabile fra 1 ms e 10 ms.
*   **R-05**: Se un evento genera una sovra-corrente che supera la soglia ma rientra entro il `T_HOLD`, l'evento NON deve far scattare il power-cycle e deve essere classificato come HCE (High Current Event).
*   **R-06**: Il sistema acquisisce e salva la traccia di corrente durante l'intervallo `T_HOLD + T_ON`, sia per gli eventi SEL veri che per gli HCE.
*   **R-07**: È disponibile un comando manuale ON/OFF per pilotare lo switch indipendentemente dallo stato del DUT (Override).
*   **R-08**: Viene garantito un log continuo del consumo di corrente con frequenza 10 Hz durante l'intero test per tracciare micro-latchup e current-steps.

### Parametri Operativi

| Parametro | Simbolo | Range | Risoluzione Tipica |
|-----------|---------|-------|--------------------|
| Soglia di corrente | `I_TH` | 1 mA – 50 mA | ≤ 0.1 mA (12-bit DAC) |
| Tempo di hold | `T_HOLD` | 1 ms – 10 ms | ≤ 100 µs |
| Tempo di OFF (power cycle) | `T_ON` | 1 ms – 10 ms | ≤ 100 µs |
| Frequenza log lento | `f_log` | 10 Hz (fissa) | ± 1 ms (timestamp) |
| Tensione alimentazione DUT | `V_DD` | 0 – 6 V (max op.)| Fissata da TPS22810 |
| Temperatura DUT | `T_DUT` | +85 °C | ± 2 °C |

## 🧪 Test del Sistema (Simulazione Hardware-in-the-loop)

Per testare il sistema senza il setup finale (INA301, TPS22810, e DUT fisico), è possibile effettuare una simulazione usando la STM32Nucleo e la GUI:

1. **Test Connettività e Log 10Hz**
   - Assicurarsi che l'IP della Nucleo corrisponda ai parametri `HOST` e `PORT` in `antisel_dashboard_eth.py`.
   - Inviare il comando `PING` dalla GUI e verificare la risposta `PONG`.
   - Il firmware della Nucleo può essere programmato per inviare periodicamente `LOG_10HZ <dati>`; verificare che la GUI lo mostri nel pannello a destra "Log 10Hz & Tracce".

2. **Test Impostazione Soglie (Output DAC)**
   - Nel tab **AntiSEL Config**, regolare lo slider `I_TH`. La GUI calcola i counts necessari in base a `R_SHUNT` e al `GAIN`.
   - La GUI invia `DAC_SET <counts>`.
   - Con un multimetro, misurare l'uscita analogica del DAC sulla STM32Nucleo per assicurarsi che produca correttamente la `V_LIMIT`.
   - Impostare `T_HOLD` e `T_ON` per verificare l'invio corretto di `THOLD_SET` e `TON_SET`.

3. **Simulazione Macchina a Stati (SEL vs HCE)**
   - Collegare un pulsante sul pin di input della Nucleo destinato al segnale `ALARM` dell'INA301.
   - Monitorare con un oscilloscopio il pin `GPIO_EN` (uscita per il TPS22810).
   - Simulare un allarme prolungato (più di `T_HOLD`): il `GPIO_EN` deve abbassarsi per un tempo pari a `T_ON` per poi tornare alto (evento SEL).
   - Simulare un allarme breve (meno di `T_HOLD`): il `GPIO_EN` non deve subire variazioni (evento HCE).

4. **Simulazione Tracce Veloci**
   - Alla ricezione di un allarme simulato, far inviare alla Nucleo via TCP un pacchetto delimitato da `TRACE_START <tipo_evento>` e `TRACE_END`.
   - Verificare che la GUI salvi i dati intermedi in un file CSV `trace_YYYYMMDD_HHMMSS.csv` localmente, isolandoli dal log real-time.

## 🔧 Sviluppo

### Struttura codice
- `__init__`: Inizializzazione GUI e socket
- `_build_ui()`: Costruzione interfaccia Tkinter
- `_connect() / _disconnect()`: Gestione connessione TCP
- `_rx_loop()`: Thread lettura socket
- `_send_cmd()`: Invio comandiTCP
- `_ping_loop_thread()`: Thread loop automatico PING
- `_log()`: Gestione logging con tag colori



## 📞 Support

Per problemi di connessione:
1. Verificare indirizzo IP e porta
2. Controllare raggiungibilità dispositivo (ping)
3. Controllare log errori applicazione
