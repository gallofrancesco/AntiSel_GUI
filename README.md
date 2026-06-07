# AntiSEL Dashboard v2.0

Applicazione grafica per il controllo e il monitoraggio di dispositivi **AntiSEL** via comunicazione Ethernet TCP/IP.

## 📋 Descrizione

Dashboard interattiva basata su **Tkinter** che consente di:
- Comunicare con dispositivo NUCLEO-H755ZI-Q via TCP socket
- Monitorare lo stato della connessione in tempo reale
- Inviare comandi predefiniti e personalizzati
- Visualizzare metriche di comunicazione (RTT, pacchetti TX/RX, errori)
- Eseguire loop automatico di ping con misurazione latenza
- Mantenere log cronologico di tutte le operazioni

## 🖥️ Requisiti

- **Python** 3.7+
- **Tkinter** (incluso in Python standard)
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
- **Log comunicazione**: Cronologia completa di TX/RX con color-coding
- **Pannello comandi**: Pulsanti azioni rapide + entry per comandi personalizzati
- **Toggle Ping Loop**: Esecuzione automatica di PING ogni secondo con misurazione latenza

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

## 🔧 Sviluppo

### Struttura codice
- `__init__`: Inizializzazione GUI e socket
- `_build_ui()`: Costruzione interfaccia Tkinter
- `_connect() / _disconnect()`: Gestione connessione TCP
- `_rx_loop()`: Thread lettura socket
- `_send_cmd()`: Invio comandiTCP
- `_ping_loop_thread()`: Thread loop automatico PING
- `_log()`: Gestione logging con tag colori

### Estensioni possibili
- Persistenza configurazione (JSON/INI)
- Export log su file
- Storico metriche con grafici
- Multi-device support
- Implementazione protocollo customizzato

## 📄 Licenza

[Specifica licenza se applicabile]

## 👤 Autore

Sviluppato per il controllo e debug dispositivi AntiSEL.

## 📞 Support

Per problemi di connessione:
1. Verificare indirizzo IP e porta
2. Controllare raggiungibilità dispositivo (ping)
3. Verificare firewall
4. Controllare log errori applicazione
