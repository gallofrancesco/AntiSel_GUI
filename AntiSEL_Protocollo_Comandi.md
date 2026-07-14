# Protocollo comandi AntiSEL — v5 (Fase 1: transparent mode + config runtime)

Documento di contratto tra **firmware CM7** (server TCP) e **dashboard Python**
(`antisel_dashboard_eth.py`, client TCP).

- **Trasporto:** TCP, `192.168.1.100:7755`
- **Encoding:** ASCII, un comando per riga, terminatore `\r\n`
- **Risposte:** una riga `OK ...` oppure `ERR <causa>`
- **Case:** i verbi sono maiuscoli; il parser accetta spazi multipli come separatori
- **Telemetria asincrona:** il firmware invia spontaneamente righe `LOG_10HZ`,
  `TRACE_START`, campioni traccia e `TRACE_END` (vedi §4)

Legenda stato comando:

- 🟢 **NEW** — introdotto in Fase 1
- 🟡 **CHANGED** — esisteva, cambia sintassi o risposta
- ⚪ **KEPT** — invariato rispetto al firmware attuale
- 🔵 **ALIAS** — accettato per retrocompatibilità durante la migrazione

---

## 1. Configurazione elettrica e parametrica

I parametri elettrici (gain, shunt, VREF) diventano **runtime** e vengono
inviati dalla GUI alla connessione. Il firmware diventa l'**unica fonte di
verità**: fa le conversioni mA↔conteggi, valida i range e annota tracce/eventi.

| Comando | Stato | Argomento | Range / validazione | Risposta OK |
|---|---|---|---|---|
| `SET GAIN <v>` | 🟢 NEW | gain INA301 [V/V] | {20, 50, 100} (A1/A2/A3) | `OK GAIN=<v>` |
| `SET RSHUNT <ohm>` | 🟢 NEW | resistenza shunt [Ω] | > 0, ≤ 100 | `OK RSHUNT=<ohm>` |
| `SET VREF_ADC <v>` | 🟢 NEW | riferimento ADC [V] | 1.0 … 3.6 | `OK VREF_ADC=<v>` |
| `SET VREF_DAC <v>` | 🟢 NEW | riferimento DAC [V] | 1.0 … 3.6 | `OK VREF_DAC=<v>` |
| `SET THRESHOLD_MA <mA>` | 🟢 NEW | soglia I_TH [mA] | 1.0 … 50.0 (R-02) **e** VLIMIT nel range elettrico | `OK THRESHOLD_MA=<mA> DAC=<counts> VLIMIT=<V>` |
| `SET THOLD_US <us>` | 🟡 CHANGED | T_HOLD [µs] | 1000 … 10000, passo ≤ 100 (R-03) | `OK THOLD_US=<us>` |
| `SET TON_US <us>` | 🟡 CHANGED | T_ON [µs] | 1000 … 10000, passo ≤ 100 (R-04) | `OK TON_US=<us>` |
| `SET RETRY_MAX <n>` | 🟡 CHANGED | max riarmi consecutivi | 1 … 100 | `OK RETRY_MAX=<n>` |
| `SET TCLEAR_MS <ms>` | 🟡 CHANGED | finestra "pulito" | 1 … 10000 | `OK TCLEAR_MS=<ms>` |
| `GET CONFIG` | 🟢 NEW | — | — | vedi sotto |

Risposta a `GET CONFIG` (riga singola, `key=value` separati da spazio):

```
OK CONFIG GAIN=20 RSHUNT=1.000 VREF_ADC=3.300 VREF_DAC=3.300 THRESHOLD_MA=10.0 DAC=248 VLIMIT=0.200 THOLD_US=5000 TON_US=2000 RETRY_MAX=3 TCLEAR_MS=30
```

### Conversioni (eseguite dal firmware)

```
VLIMIT   = THRESHOLD_MA/1000 · RSHUNT · GAIN            [V]
DAC_CODE = round(VLIMIT / VREF_DAC · DAC_FULL_SCALE)    (saturato 0…4095)
IDUT     = (ADC_CODE/ADC_FULL_SCALE · VREF_ADC) / (GAIN · RSHUNT)   [A]
```

Costanti: `DAC_FULL_SCALE = 4095` (12 bit), `ADC_FULL_SCALE = 65535` (16 bit).

**Validazione `SET THRESHOLD_MA`:** se `DAC_CODE` satura o `VLIMIT` esce dal
range elettrico consentito, il firmware risponde `ERR RANGE` e **non** applica
il nuovo valore (la soglia precedente resta attiva).

---

## 2. Controllo del DUT e della protezione

La priorità degli eventi (spec §7) è applicata dal firmware: un `SWITCH ON`
**non** può scavalcare un SEL attivo, uno stato FAULT o un ciclo T_ON in corso.

| Comando | Stato | Effetto | Risposta |
|---|---|---|---|
| `SWITCH OFF` | 🟢 NEW | Override manuale OFF (massima priorità): apre lo switch da qualsiasi stato, il DUT resta spento finché non arriva un `SWITCH ON` esplicito | `OK SWITCH=OFF` |
| `SWITCH ON` | 🟢 NEW | Chiude lo switch **solo se** lecito (no SEL attivo, no FAULT, no T_ON) | `OK SWITCH=ON` oppure `ERR <FAULT\|SEL_ACTIVE\|BUSY>` |
| `ACK FAULT` | 🟢 NEW | Acknowledge esplicito richiesto per uscire da FAULT (predisposto per Fase 2) | `OK` oppure `ERR NOT_IN_FAULT` |
| `RESET` | ⚪ KEPT | Azzera contatori, riarma la protezione, richiude lo switch | `OK RESET` |
| `INA_RST` | ⚪ KEPT | Impulso di reset INA301 — **diagnostico** (utile solo in modalità latched); in transparent mode è un no-op sicuro | `OK INA_RST` |
| `DUT_ON` | 🔵 ALIAS | = `SWITCH ON` | come `SWITCH ON` |
| `DUT_OFF` | 🔵 ALIAS | = `SWITCH OFF` | come `SWITCH OFF` |

---

## 3. Interrogazione stato

| Comando | Stato | Risposta |
|---|---|---|
| `PING` | ⚪ KEPT | `PONG` |
| `GET STATUS` | 🟡 CHANGED | `OK STATUS=<nome> ALERT=<0\|1> SWITCH=<ON\|OFF> OVERRIDE=<0\|1> RETRY=<n> SEL=<n> HCE=<n>` |
| `GET COUNTERS` | 🟢 NEW | `OK SEL=<n> HCE=<n>` |
| `STATUS` | 🔵 ALIAS | = `GET STATUS` |

`ALERT=1` significa allarme attivo (pin ALERT basso, sovracorrente in corso).
`OVERRIDE=1` significa che è attivo un override manuale OFF.

---

## 4. Telemetria asincrona (firmware → GUI)

### 4.1 Log lento 10 Hz (spec §11)

Una riga ogni 100 ms, formato `key=value` separati da spazio:

```
LOG_10HZ TICK=<ms> ADC=<raw> VOUT=<V> I_MA=<mA> THR_MA=<mA> ALERT=<0|1> SWITCH=<ON|OFF> OVERRIDE=<0|1> STATE=<n> RETRY=<n> SEL=<n> HCE=<n> FRESH=<0|1>
```

Esempio:

```
LOG_10HZ TICK=123400 ADC=1024 VOUT=0.825 I_MA=8.250 THR_MA=20.0 ALERT=1 SWITCH=ON OVERRIDE=0 STATE=1 RETRY=0 SEL=2 HCE=14 FRESH=1
```

`FRESH=0` indica dato stantìo (DMA fermo durante l'invio di una traccia).
`STATE` è l'indice numerico della macchina a stati (vedi §5).

### 4.2 Traccia evento (spec §6)

Sequenza inviata alla chiusura di un evento HCE o SEL:

```
TRACE_START <SEL|HCE> FS=<Sa/s> N=<campioni> THOLD_US=<us> TON_US=<us> DAC=<counts> TRIG=<idx> TICK=<ms>
<idx>,<adc_raw>
<idx>,<adc_raw>
...
TRACE_END
```

`TRIG` è l'indice (relativo all'inizio traccia) del campione di trigger, così la
GUI può marcare pre-trigger vs post-trigger. La conversione in mA la fa la GUI
con la config nota, oppure il firmware include già `I_MA` (deciso in Fase 2).

---

## 5. Macchina a stati — indici `STATE`

**Fase 2 (attiva):** macchina a **11 stati** della spec §6. `STATE_NAMES`
nella dashboard è stato esteso in parallelo. Aggiunti i comandi `GET EVENT <id>`
e `ACK FAULT` (vedi §2/§3).

| Indice | Fase 1 (attuale) | Fase 2 (spec §6) |
|---|---|---|
| 0 | `IDLE` | `INIT` |
| 1 | `THOLD` | `IDLE` |
| 2 | `TON` | `ALARM` |
| 3 | `PERMANENT_OFF` | `HOLD_RUN` |
| 4 | `COOLDOWN` | `HCE_SAVE` |
| 5 | — | `CUTOFF` |
| 6 | — | `TON_RUN` |
| 7 | — | `RECOVERY` |
| 8 | — | `VERIFY` |
| 9 | — | `MANUAL_OFF` |
| 10 | — | `FAULT` |

> ⚠️ Alla transizione a Fase 2 questa tabella cambia: la GUI deve aggiornare
> `STATE_NAMES` e la logica colore stato nello **stesso** commit del firmware.

---

## 6. Gestione errori

- Comando sconosciuto → `ERR UNKNOWN` (il firmware attuale rispondeva `ACK`:
  🟡 CHANGED, per rispettare la spec §13 "risposte OK oppure ERROR con causa").
- Argomento mancante o non numerico → `ERR ARG`
- Valore fuori range → `ERR RANGE`
- Azione non lecita nello stato corrente → `ERR <FAULT|SEL_ACTIVE|BUSY|NOT_IN_FAULT>`

Tutte le stringhe ricevute sono validate prima dell'uso (spec §13): nessuna
stringa grezza viene passata a funzioni che la interpretano senza controllo di
lunghezza e formato.

---

## 7. Sequenza tipica alla connessione (lato GUI)

```
-> SET VREF_ADC 3.3
-> SET VREF_DAC 3.3
-> SET GAIN 20
-> SET RSHUNT 1.0
-> SET THRESHOLD_MA 10.0      <- il firmware calcola e applica il DAC
-> SET THOLD_US 5000
-> SET TON_US 2000
-> SET RETRY_MAX 3
-> SET TCLEAR_MS 30
-> GET CONFIG                 <- rilettura di conferma
```

Da qui in poi il firmware emette `LOG_10HZ` a 10 Hz e le tracce agli eventi.

---

## 8. Modifiche richieste alla dashboard `antisel_dashboard_eth.py`

Da fare **in parallelo** all'aggiornamento firmware di Fase 1:

1. Alla connessione, inviare la sequenza §7 (invio config invece di sola
   conversione locale).
2. Soglia: sostituire l'invio di `DAC_SET <counts>` con `SET THRESHOLD_MA <mA>`;
   la conversione la fa ora il firmware (la GUI può ancora mostrare il DAC
   stimato in anteprima).
3. Tempi: `THOLD_SET <ms>` / `TON_SET <ms>` → `SET THOLD_US <us>` / `SET TON_US <us>`
   (moltiplicare per 1000 il campo ms).
4. `RETRY_SET`/`TCLEAR_SET` → `SET RETRY_MAX`/`SET TCLEAR_MS`.
5. Parser `LOG_10HZ`: leggere i nuovi campi (`I_MA`, `THR_MA`, `VOUT`, `ALERT`,
   `SWITCH`, `OVERRIDE`) e usarli direttamente invece di riconvertire i conteggi
   lato PC.
6. `STATE_NAMES`: invariato in Fase 1; da estendere a 11 voci in Fase 2.

---

_Versione documento: v5 — Fase 1. Aggiornare a ogni modifica di protocollo._
