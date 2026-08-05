# AntiSEL Dashboard

Applicazione grafica per il controllo e il monitoraggio di dispositivi
**AntiSEL** via comunicazione Ethernet TCP/IP.

Dashboard interattiva basata su **CustomTkinter** che consente di comunicare
con la NUCLEO-H755ZI-Q via TCP socket, monitorare lo stato della connessione,
inviare comandi, visualizzare grafici in tempo reale (log 10 Hz e tracce
evento SEL/HCE) e mantenere un log cronologico di tutte le operazioni.
Include inoltre un pannello **PID CTRL / RTU** (placeholder, connessione TCP
indipendente) per il monitoraggio della temperatura del DUT e l'impostazione
del setpoint, in previsione dell'integrazione hardware descritta in Figura 1
della [descrizione di sistema](docs/AntiSEL_System_Description.pdf).

## Avvio rapido

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python antisel_dashboard_eth.py
```

L'indirizzo IP, la porta e il timeout di connessione si configurano in cima
a `antisel_dashboard_eth.py` (`HOST`, `PORT`, `TIMEOUT`).

## Documentazione

- **[Guida utente](docs/GUIDA_UTENTE.md)** — installazione, layout
  dell'interfaccia, uso di ogni pannello, file CSV generati, risoluzione
  problemi.
- **[Protocollo comandi](docs/AntiSEL_Protocollo_Comandi.md)** — contratto
  di comunicazione TCP tra firmware e dashboard.
- **[Descrizione di sistema](docs/AntiSEL_System_Description.pdf)** —
  requisiti funzionali del sistema AntiSEL (INA301 Latch Mode).
- **[Descrizione del flusso](docs/AntiSEL_Flow_Description_G.docx)** —
  macchina a stati e flusso operativo.
- **[Datasheet INA301](docs/ina301.pdf)** — riferimento hardware del
  componente di rilevamento sovracorrente.

## Requisiti Sistema AntiSEL (INA301 Latch Mode)

*   **R-01**: Monitoraggio e protezione della corrente di alimentazione del DUT (Device Under Test) mediante un sistema di rilevamento a soglia basato su INA301 in modalità Latch.
*   **R-02**: Soglia di intervento (`I_TH`) regolabile in funzione delle esigenze del componente, in un range nominale di 1 mA – 50 mA.
*   **R-03**: Tempo `T_HOLD` (intervallo fra il superamento della soglia e l'apertura dello switch) selezionabile fra 1 ms e 10 ms.
*   **R-04**: Tempo `T_ON` (intervallo fra l'apertura e la richiusura dello switch) selezionabile fra 1 ms e 10 ms.
*   **R-05**: Se un evento genera una sovra-corrente che supera la soglia ma rientra entro il `T_HOLD`, l'evento NON deve far scattare il power-cycle e deve essere classificato come HCE (High Current Event).
*   **R-06**: Il sistema acquisisce e salva la traccia di corrente durante l'intervallo `T_HOLD + T_ON`, sia per gli eventi SEL veri che per gli HCE.
*   **R-07**: È disponibile un comando manuale ON/OFF per pilotare lo switch indipendentemente dallo stato del DUT (Override).
*   **R-08**: Viene garantito un log continuo del consumo di corrente con frequenza 10 Hz durante l'intero test per tracciare micro-latchup e current-steps.

| Parametro | Simbolo | Range | Risoluzione Tipica |
|-----------|---------|-------|--------------------|
| Soglia di corrente | `I_TH` | 1 mA – 50 mA | ≤ 0.1 mA (12-bit DAC) |
| Tempo di hold | `T_HOLD` | 1 ms – 10 ms | ≤ 100 µs |
| Tempo di OFF (power cycle) | `T_ON` | 1 ms – 10 ms | ≤ 100 µs |
| Frequenza log lento | `f_log` | 10 Hz (fissa) | ± 1 ms (timestamp) |
| Tensione alimentazione DUT | `V_DD` | 0 – 6 V (max op.)| Fissata da TPS22810 |
| Temperatura DUT | `T_DUT` | +85 °C | ± 2 °C |

## Support

Per problemi di connessione:
1. Verificare indirizzo IP e porta.
2. Controllare raggiungibilità dispositivo (ping).
3. Controllare log errori applicazione.

Vedi anche la sezione "Risoluzione problemi" nella [guida utente](docs/GUIDA_UTENTE.md#8-risoluzione-problemi).
