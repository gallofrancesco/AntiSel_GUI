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
> ⚠️ **SPI3 rimappato su PC10/PC11/PC12**: la mappatura originale
> (PB3/PB4/PB5) condivideva SCK/MISO con i pin JTAG (JTDO-TRACESWO,
> NJTRST). In campo, `GET RAW` ha mostrato scritture di registro
> "scivolate" su un indirizzo adiacente durante `MAX31856_Init()`
> (es. il valore atteso in CR1 ritrovato in MASK) — sintomo di un
> glitch elettrico/di timing sul bus SPI3. Spostato su PC10/PC11/PC12
> (connettore ZIO **CN8**, non condivisi con nessuna altra funzione né
> con pin storicamente JTAG) per eliminarlo alla radice. **Richiede
> ricablaggio fisico** del modulo MAX31856 dal vecchio SPI3 su CN7 al
> nuovo SPI3 su CN8. Il CS resta su PE4, connettore **CN9** (invariato
> — corretto anche il riferimento errato a CN7 presente in una versione
> precedente di questa tabella). Vedi anche il fix "read-back + retry" in
> `MAX31856_Init()`, mantenuto come mitigazione software indipendente.

## Segnali applicativi

| Segnale | Pin STM32 | Connettore | Periferica / config | Ruolo |
|---|---|---|---|---|
| MAX31856 CS | **PE4** | CN9 | GPIO output PP, riposo **HIGH** (attivo basso) | Chip select SPI del driver termocoppia. **Fix**: il livello di uscita iniziale nel `.ioc`/`gpio.c` era rimasto `GPIO_PIN_RESET` (LOW) dalla vecchia mappatura RTD, tenendo il CS asserito dal boot fino alla prima `cs_high()` in `MAX31856_Init()` — durante la finestra di `MX_LWIP_Init()` (autonegoziazione PHY, anche secondi). Corretto a `GPIO_PIN_SET` (HIGH) |
| MAX31856 SCK | **PC10** | CN8 (pin 6, `D45`) | SPI3_SCK, AF6 | Rimappato da PB3 (JTDO/TRACESWO) — vedi nota sopra |
| MAX31856 MISO | **PC11** | CN8 (pin 8, `D46`) | SPI3_MISO, AF6 | Rimappato da PB4 (NJTRST) — vedi nota sopra |
| MAX31856 MOSI | **PC12** | CN8 (pin 10, `D47`) | SPI3_MOSI, AF6 | Rimappato da PB5. SPI3: 8 bit, mode 1 (CPOL=0/CPHA=1), prescaler /64 (3.125 MBit/s, da CubeMX). Sostituisce la precedente mappatura su SPI2 (PB10/PC2_C/PC3_C) per evitare i pin "_C" (switch analogico) e il LED LD3 (PB14) |
| Heater PWM | **PA6** | CN7 (`D12`) | TIM3_CH1, AF2 | Pilota lo stadio di potenza (MOSFET/SSR — tipo ancora TBD, §7 proposta) verso le resistenze. Avviato in `Heater_Init()` (`HAL_TIM_PWM_Start`), duty scritto via `__HAL_TIM_SET_COMPARE` |

> Nota: PC10/PC11/PC12 sul connettore CN8 sono etichettati `SDMMC_D2/D3/CK`
> nel silkscreen ZIO del Nucleo (funzione alternativa non usata in questo
> progetto) — è normale che appaiano con quel nome sulla serigrafia della
> scheda pur essendo qui configurati come SPI3.

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
