# Sensor de temperatura Wi-Fi para refrigeradores e câmaras frias

Sensor sem fio de ultra baixo consumo que mede a temperatura de refrigeradores,
freezers e câmaras frias, envia pelo Wi-Fi que já existe no local e roda **anos
com as mesmas pilhas**. Um servidor simples (Python, sem dependências) mostra o
painel, guarda o histórico, exporta CSV e dispara alertas.

```
 ┌────────── câmara fria / refrigerador ──────────┐
 │   sonda SHT4x (inox)                             │
 │        │ cabo fino plano passando pela borracha  │
 └────────┼─────────────────────────────────────────┘
          │
   ┌──────┴───────────┐   Wi-Fi 2,4 GHz   ┌──────────┐      ┌──────────────────────┐
   │ ESP32-C3 + pilhas│ ─────────────────▶│ roteador │ ───▶ │ servidor.py (painel, │
   │ (fora do frio)   │ 1 lote por hora,  └──────────┘      │ CSV, alertas Telegram)│
   └──────────────────┘ alarme na hora                      └──────────────────────┘
```

## Como a bateria dura anos usando Wi-Fi

O Wi-Fi consome de 80 a 350 mA quando está ligado, e isso esgota qualquer pilha em
dias se ele ficar ativo. O segredo é **quase nunca ligar o rádio**:

1. **Deep sleep em > 99,9 % do tempo.** O ESP32-C3 dorme com ~5 µA. O circuito todo
   fica em ~9 µA, porque o sensor é desligado por um pino de GPIO e o regulador tem
   Iq de 2 µA.
2. **A medição não liga o Wi-Fi.** A cada 5 min o chip acorda, lê o SHT4x (~10 ms),
   guarda a leitura na memória RTC e volta a dormir.
3. **Envio em lote.** As 12 leituras da última hora saem juntas numa única conexão.
4. **Conexão relâmpago.** IP fixo (sem DHCP) e canal e BSSID do roteador guardados
   deixam o rádio ligado por poucas centenas de ms, não 2 a 5 s. O envio é um HTTP
   mínimo para um servidor na rede local (TLS custaria muito mais energia).
5. **Alarme imediato.** Se a temperatura sai da faixa por 3 leituras seguidas, o
   sensor envia **na hora**, sem esperar o lote. Enquanto o alarme durar, ele mede a
   cada 2 min e reenvia a cada 10 min.
6. **Rede fora do ar não drena a bateria.** As leituras ficam guardadas (até 24 h) e
   as novas tentativas seguem uma espera crescente (5, 10, 20… até 60 min).

### Estimativa de autonomia

`ferramentas/autonomia.py` faz a conta com valores conservadores: 9 µA dormindo,
5 mA·s por medição e 100 mA·s por envio.

| Configuração | Consumo | 3× AA lítio (3000 mAh) | 1× Li-SOCl₂ tamanho D (19 Ah) |
|---|---|---|---|
| mede 5 min, envia a cada **60 min** | 468 mAh/ano | **~4,8 anos** | 10+ anos* |
| mede 5 min, envia a cada 30 min | 712 mAh/ano | ~3,2 anos | 10+ anos* |
| mede 5 min, envia a cada 15 min | 1198 mAh/ano | ~2,0 anos | 10+ anos* |

\* O limite passa a ser a vida útil da própria pilha, não o consumo.

O Wi-Fi responde por 50 a 80 % do gasto. **Cada envio a menos por dia vale mais que
qualquer outra otimização.** Meça o seu protótipo e rode o script com os seus números:

```bash
python3 ferramentas/autonomia.py --medicao-min 5 --envio-min 60 --sono-ua 9 --envio-mas 100
```

## Lista de materiais (por sensor)

| Item | Sugestão | Por quê |
|---|---|---|
| Microcontrolador | Módulo **ESP32-C3-MINI-1** (ou ESP32-C3 SuperMini sem o LED) | Wi-Fi, ~5 µA em deep sleep, barato |
| Sensor | **Sensirion SHT40/SHT45** em sonda encapsulada com cabo | ±0,2 °C (SHT45 ±0,1 °C), –40 a 125 °C, 0,08 µA parado, lê em 9 ms |
| Regulador 3,3 V | **RT9080-33** (Iq 2 µA, 600 mA) ou TPS7A02 / MCP1700 | Iq baixíssimo; aguenta os picos do Wi-Fi |
| Capacitores | 470–1000 µF eletrolítico de baixa ESR + 10 µF cerâmico na saída de 3,3 V | Seguram os picos de ~350 mA do Wi-Fi |
| Pilhas | **3× AA de lítio (Energizer Ultimate Lithium L91)** | Funcionam até –40 °C, 20 anos de prateleira, sem vazamento |
| Alternativa 10+ anos | 1× Li-SOCl₂ D (ER34615, 3,6 V 19 Ah) + capacitor híbrido (HLC) ou supercap | Exige capacitor de pulso: essa química não entrega os picos do Wi-Fi sozinha |
| Divisor da bateria | 4,7 MΩ + 2,2 MΩ + 100 nF | Consome só ~0,8 µA |
| Caixa | IP65 com prensa-cabo | Condensação é comum na saída da câmara |

> **Evite placas de desenvolvimento como estão.** O LED de power, o chip USB-serial e o
> regulador genérico (AMS1117) consomem de 40 µA a 5 mA dormindo e matam a bateria em
> semanas. Use o módulo nu ou uma placa de baixo consumo, remova o LED, alimente
> pelo pino 3V3 a partir do RT9080 e **meça a corrente em sono** com multímetro na
> escala de µA antes de instalar.

## Ligações (ESP32-C3)

```
Pilhas 3×AA (+) ──┬── RT9080 VIN      RT9080 VOUT (3,3 V) ──┬── 3V3 do ESP32-C3
                  │                                          ├── 1000 µF + 10 µF ── GND
                  ├── 4,7 MΩ ──┬── GPIO2 (ADC)
                  │           ├── 2,2 MΩ ── GND
                  │           └── 100 nF ── GND
GPIO3 ─────────── VDD do SHT4x  e  pull-ups de 10 kΩ do SDA/SCL   (sensor sem energia no sono)
GPIO6 ─────────── SDA do SHT4x
GPIO7 ─────────── SCL do SHT4x
GND ───────────── GND do SHT4x
```

Os pull-ups do I2C **precisam** ser alimentados pelo GPIO3, e não pelo 3,3 V fixo.
Senão eles alimentam o sensor por fora durante o sono e consomem corrente à toa.

## Instalação física (onde a maioria dos projetos falha)

- **A eletrônica fica fora do frio.** Refrigerador e câmara fria são caixas de metal
  que bloqueiam o Wi-Fi, e pilha no frio perde capacidade. Só a sonda entra; o cabo
  passa pela borracha da porta (use cabo plano fino) ou por um furo vedado com
  silicone.
- **Posição da sonda.** Coloque no centro do volume de carga, longe do evaporador,
  da saída de ar e da porta.
- **Evite alarmes falsos ao abrir a porta.** O firmware só alarma após 3 leituras
  seguidas fora da faixa (15 min). Em vacinas e alimentos, é comum colocar a sonda
  dentro de um frasco com glicol ou areia: ele imita a temperatura do produto, não a
  do ar.
- **Calibração.** Teste em banho de gelo picado com água (0,0 °C) e compare com um
  termômetro de referência. Para uso regulado (vigilância sanitária, farmácia,
  vacinas), use sensores com certificado de calibração rastreável (RBC/Inmetro).

## Firmware

Em `firmware/` (PlatformIO + Arduino):

```bash
cd firmware
cp include/segredos.exemplo.h include/segredos.h   # Wi-Fi, IP do servidor e token
# ajuste include/config.h: IP fixo de cada sensor, faixa de temperatura, intervalos
pio run -t upload
```

- **Cada sensor precisa de um IP fixo diferente** (`IP_FIXO`). Reserve uma faixa
  fora do DHCP do roteador.
- Configure a faixa por equipamento: refrigerador 0 a 5 °C, freezer –25 a –15 °C,
  vacinas 2 a 8 °C.
- Para depurar pela USB, descomente as linhas `DEPURACAO` no `platformio.ini`. Isso
  consome muito mais energia; volte a comentar antes de instalar.
- O sensor é identificado pelo MAC. Ao ser ligado, envia um pacote na hora para
  confirmar a instalação.

## Servidor e painel

Em `servidor/` (Python 3, só biblioteca padrão). Roda num Raspberry Pi, num PC ou
num servidor da rede local:

```bash
SENSOR_TOKEN=troque-este-token \
TELEGRAM_TOKEN=123:abc TELEGRAM_CHAT_ID=987654 \
python3 servidor/servidor.py --porta 8080
```

- **Painel** em `http://IP-do-servidor:8080/`, com temperatura atual, status, gráfico
  de 24 h, bateria, sinal e último contato. Atualiza sozinho a cada minuto.
- **Alertas** no terminal e, opcionalmente, no Telegram:
  - temperatura fora da faixa e normalização;
  - **sensor sem comunicação** (sem pacote em 2× o intervalo de envio + 15 min),
    que avisa quando a pilha acaba ou o Wi-Fi cai;
  - bateria baixa (padrão < 3,7 V nas 3 pilhas; ajuste com `BATERIA_BAIXA_MV`).
- **CSV** por sensor em `/csv?sensor=ID&dias=30`, para planilhas de controle e
  fiscalização.
- **Nomear sensores:**
  `curl -H "X-Token: ..." -d '{"id":"A1B2C3D4E5F6","nome":"Câmara de congelados"}' http://IP:8080/api/nome`

Retransmissões não duplicam leituras: cada leitura tem uma chave própria
(sensor + boot + instante).

## Próximos passos

- **ESP32-C6 com o núcleo LP:** o coprocessador de baixo consumo lê o sensor sem
  acordar o processador principal e corta o custo das medições (hoje ~30 % do
  gasto). Com ele, 5+ anos em AA ficam folgados mesmo com envio a cada 30 min.
- **Placa própria (PCB):** junta módulo, regulador, divisor e conector da sonda numa
  placa pequena, com consumo previsível.
- **Onde o Wi-Fi não alcança, ou para mais de 10 anos:** trocar o rádio por LoRa
  (ou Zigbee/BLE) com um gateway. É o caminho que muitos sistemas comerciais de
  longa duração usam, porque esses rádios gastam uma fração da energia do Wi-Fi.

## Estado de validação

- O servidor foi testado de ponta a ponta com lotes simulados: recebimento,
  desduplicação, alarme, bateria baixa, CSV e token inválido.
- O firmware foi revisado e verificado quanto à sintaxe, mas **ainda não foi
  compilado com o toolchain do ESP32 nem testado em hardware**. Os números de
  autonomia são estimativas e precisam ser confirmados com medição no protótipo.
