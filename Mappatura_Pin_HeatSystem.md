# Mappatura pin Heat System — NUCLEO-H753ZI

Scheda **fisicamente separata** dal Nucleo AntiSEL (192.168.1.100, porta
7755) — vedi `AntiSEL/docs/Proposta_HeatSystem_RTU_PID.md`. Tutta la logica
gira su questo MCU single-core.

> ⚠️ **Chip aggiornato**: l'hardware montato è un **MAX31856** (termocoppia
> Tipo T, Adafruit) e **non** un MAX31865 (RTD) come indicato in una
> versione precedente di questa scheda — protocollo/registri dei due chip
> sono incompatibili. Il driver firmware è `Core/Src/max31856.c` /
> `Core/Inc/max31856.h`.
>
> ⚠️ **Storico SPI3/PC10-12 abbandonato**: la mappatura precedente
> (SPI3 su PC10/PC11/PC12, connettore CN8) mostrava scritture di registro
> "scivolate" su un indirizzo adiacente durante `MAX31856_Init()`
> (es. il valore atteso in CR1 ritrovato in MASK), e diagnostica hardware
> approfondita (comandi `GET RAW`/`GET GPIO`/`TEST SCK` aggiunti per il
> bring-up) ha mostrato SCK fermo a livello alto invece che al riposo
> basso atteso (CPOL=0) anche forzando il toggle via GPIO puro — sintomo
> di un problema sul periferico/pin SPI3 stesso, non risolto dal
> ricablaggio CN7→CN8. **Migrato a SPI1 sul connettore ZIO CN7**
> (Table 18 del datasheet ufficiale, `docs/um2407-...pdf`), che espone
> SCK/MISO/MOSI in modo nativo senza passare da AF alternativi condivisi
> con JTAG o SDMMC. Il CS, inizialmente lasciato su PE4 (**CN9**, non CN7
> — vedi nota precedente sulla correzione di questo riferimento), è stato
> **spostato anch'esso su CN7** (PD14, pin `SPI_A_CS` nativo) dopo aver
> misurato PE4 fermo a 0V esternamente nonostante il firmware lo leggesse
> `HIGH` — vedi dettaglio nella tabella sotto. **Richiede ricablaggio
> fisico** del modulo MAX31856 da CN8/CN9 interamente a CN7.
>
> ⚠️ **Conflitto risolto con Heater PWM**: MISO di SPI1 è **PA6**, lo
> stesso pin fisico che era assegnato a `Heater PWM` (`TIM3_CH1`, CN7
> `D12`). Il PWM del riscaldatore è stato spostato su **TIM4_CH4/PD15**
> (anch'esso su CN7, `D9`) per liberare PA6.
>
> Vedi anche il fix "read-back + retry" in `MAX31856_Init()`, mantenuto
> come mitigazione software indipendente.
>
> ⚠️ **Da aggiornare anche nel `.ioc`**: queste modifiche sono state
> fatte a mano in `spi.c`/`tim.c`/`heater_ctrl.c`/`main.c`, fuori dai
> blocchi `USER CODE`. Se in futuro si rigenera il codice da CubeMX senza
> aver prima allineato il pinout nel `.ioc` (SPI1 su PA5/PA6/PB5, TIM4_CH4
> su PD15), la rigenerazione sovrascrive questi file e si torna alla
> vecchia mappatura SPI3/TIM3.

## Segnali applicativi

| Segnale | Pin STM32 | Connettore | Periferica / config | Ruolo |
|---|---|---|---|---|
| MAX31856 CS | **PD14** | CN7 (pin 16, `D10`) | GPIO output PP, riposo **HIGH** (attivo basso) | Chip select SPI del driver termocoppia. Spostato da PE4 (CN9 pin 16, poi da un tentativo intermedio su PE2/CN9 pin 14): PE4 misurava ripetutamente 0V esternamente nonostante il firmware leggesse `HIGH` via `GET GPIO`, con catena di misura validata (3V3 letto correttamente sullo stesso setup) — sospetta rottura del collegamento fisico su quella linea dopo il ricablaggio SPI1/CN7, non confermabile da remoto. **PD14 è il pin `SPI_A_CS` nativo di CN7** (Table 18 del datasheet) — non usato in hardware come NSS (CS resta gestito via software, `SPI_NSS_SOFT`), ma sceglierlo consolida tutti e 4 i segnali SPI (SCK/MISO/MOSI/CS) sullo stesso connettore, evitando salti tra CN7 e CN9. Nessun conflitto con TIM4_CH4 (PD15, Heater PWM): PD14 sarebbe TIM4_CH3, canale non usato. **Fix storico**: il livello di uscita iniziale nel `.ioc`/`gpio.c` era rimasto `GPIO_PIN_RESET` (LOW) dalla vecchia mappatura RTD, tenendo il CS asserito dal boot fino alla prima `cs_high()` in `MAX31856_Init()` — durante la finestra di `MX_LWIP_Init()` (autonegoziazione PHY, anche secondi). Corretto a `GPIO_PIN_SET` (HIGH) |
| MAX31856 SCK | **PA5** | CN7 (pin 10, `D13`) | SPI1_SCK, AF5 | Migrato da PC10/SPI3 (CN8) — vedi nota sopra |
| MAX31856 MISO | **PA6** | CN7 (pin 12, `D12`) | SPI1_MISO, AF5 | Migrato da PC11/SPI3. Libera PA6 dal precedente uso come Heater PWM (spostato su TIM4_CH4/PD15) |
| MAX31856 MOSI | **PB5** | CN7 (pin 14, `D11`) | SPI1_MOSI, AF5 | Migrato da PC12/SPI3. SPI1: 8 bit, mode 1 (CPOL=0/CPHA=1), prescaler /64, stesso clock kernel SPI123 via PLL già usato per SPI3 |
| Heater PWM | **PD15** | CN7 (pin 18, `D9`) | TIM4_CH4, AF2 | Pilota lo stadio di potenza (MOSFET/SSR — tipo ancora TBD, §7 proposta) verso le resistenze. Avviato in `Heater_Init()` (`HAL_TIM_PWM_Start`), duty scritto via `__HAL_TIM_SET_COMPARE`. Spostato da PA6/TIM3_CH1 per il conflitto con SPI1_MISO |

## Rete

| Parametro | Valore |
|---|---|
| IP statica | `192.168.1.101` |
| Netmask | `255.255.255.0` |
| Porta server RTU/PID | `7756` |
| MAC | `00:80:E1:xx:yy:zz` (gli ultimi 3 byte sono generati dinamicamente a runtime a partire dall'UID univoco del microcontrollore in `ethernetif.c`, garantendo l'assenza di conflitti ARP sulla LAN) |

## Clock

| Parametro | Valore |
|---|---|
| HSE | 8 MHz (MCO ST-LINK, bypass) — **non** 25 MHz come inizialmente generato da CubeMX |
| SYSCLK | 480 MHz (PLL1 M=4 N=480 P=2, VOS0, Flash Latency 4WS) |
| AHB | 240 MHz |
| APBx | 120 MHz |
| Funzione | `SystemClock_Config_480MHz()` in `Core/Src/main.c`, chiamata al posto della `SystemClock_Config()` generata — stesso schema già usato in AntiSEL |
| Alimentazione core | `PWR_LDO_SUPPLY` (non SMPS) — la NUCLEO-H753ZI non è popolata per l'alimentazione diretta da SMPS |

> ⚠️ **Trappola ad ogni rigenerazione CubeMX**: la chiamata a
> `SystemClock_Config_480MHz()` in `main()` sta fuori dai blocchi
> `USER CODE`, quindi CubeMX la sovrascrive sempre con la
> `SystemClock_Config()` di default ad ogni "Generate Code" — va
> ripristinata a mano dopo ogni rigenerazione. `RCC.SupplySource` è
> invece salvato correttamente nel `.ioc` come `PWR_LDO_SUPPLY` (fix
> applicato in data odierna, prima riportava erroneamente
> `PWR_DIRECT_SMPS_SUPPLY`), quindi la `SystemClock_Config()` generata
> di default ora eredita già il valore giusto.

## Periferiche di supporto

| Periferica | Config | Note |
|---|---|---|
| IWDG1 | Prescaler 256, Reload 249 → timeout ~2 s | Avviato **dopo** tutte le init lente (LWIP/BSP), appena prima del `while(1)`, altrimenti scade durante l'autonegoziazione PHY e causa un reset loop |
| LED verde (LD1) | BSP | Heartbeat: acceso all'ingresso nel loop, poi lampeggio ogni 500 ms — segnale visivo che il firmware è vivo, indipendente dal debugger |

## Corrispondenza nome ↔ `#define` nel firmware

| Segnale | Define (`Core/Inc/main.h`) |
|---|---|
| MAX31856 CS | `MAX31856_CS_Pin` / `MAX31856_CS_GPIO_Port` (rinominato oggi da `MAX31865_CS_*`) |

## Ancora da assegnare (non presente in questa scheda)

- Nessun pin di enable/fault dedicato per lo stadio di potenza: la
  proposta (§3.1/§4.1) non ne menziona uno esplicito — se lo stadio
  scelto ne richiede uno, va aggiunto in CubeMX e documentato qui.
