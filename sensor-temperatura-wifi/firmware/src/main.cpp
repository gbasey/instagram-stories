// Sensor de temperatura Wi-Fi de ultra baixo consumo para refrigeradores e câmaras frias.
//
// Ciclo de vida: o ESP32 passa >99,9 % do tempo em deep sleep (~5 µA). A cada
// INTERVALO_MEDICAO_S ele acorda, liga o sensor, mede em poucos milissegundos,
// guarda a leitura na memória RTC e volta a dormir SEM ligar o Wi-Fi. O rádio,
// que é o que realmente gasta bateria, só é ligado para enviar um lote de
// leituras (padrão: 1 vez por hora) ou imediatamente quando o estado de alarme muda.

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/gpio.h>
#include <esp_random.h>
#include <esp_sleep.h>
#include <sys/time.h>

#include "config.h"

#ifdef DEPURACAO
#define LOG(...) Serial.printf(__VA_ARGS__)
#else
#define LOG(...)
#endif

static const uint32_t MAGICO = 0x5E4F0001;

enum : uint8_t {
  FLAG_ERRO_SENSOR = 0x01,
  FLAG_FORA_FAIXA = 0x02,
};

struct Leitura {
  uint32_t t;         // segundos no relógio RTC do sensor (conta desde que ligou a bateria)
  int16_t temp_c100;  // temperatura em centésimos de °C
  uint8_t umid;       // umidade relativa em %
  uint8_t flags;
};

// Tudo aqui sobrevive ao deep sleep (memória RTC, 8 KB no ESP32-C3).
struct Estado {
  uint32_t magico;
  uint32_t boot;      // identificador aleatório desta "vida" da bateria
  uint32_t seq;       // número do pacote enviado
  uint16_t inicio;    // buffer circular
  uint16_t n;
  Leitura buf[BUFFER_MAX];
  bool wifi_cache;
  uint8_t canal;
  uint8_t bssid[6];
  uint8_t seq_fora;   // leituras consecutivas fora da faixa
  bool em_alarme;
  bool alarme_pendente;
  uint8_t falhas;
  uint32_t proxima_tentativa;
  uint32_t ultimo_envio;
  uint16_t perdidas;  // leituras descartadas por buffer cheio
};

RTC_DATA_ATTR static Estado st;

static uint32_t agoraS() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);  // continua contando durante o deep sleep
  return (uint32_t)tv.tv_sec;
}

// --------------------------------------------------------------------------
// Sensor SHT4x (I2C) — lido sem biblioteca para acordar o mínimo possível
// --------------------------------------------------------------------------
static uint8_t crc8(const uint8_t *d, int n) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < n; i++) {
    crc ^= d[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
  }
  return crc;
}

static void sensorLigar(bool ligar) {
  gpio_hold_dis((gpio_num_t)PINO_SENSOR_VCC);
  pinMode(PINO_SENSOR_VCC, OUTPUT);
  digitalWrite(PINO_SENSOR_VCC, ligar ? HIGH : LOW);
  if (!ligar) {
    // Mantém o pino em nível baixo durante o deep sleep: sensor e pull-ups sem energia.
    gpio_hold_en((gpio_num_t)PINO_SENSOR_VCC);
  }
}

static bool lerSHT4x(float &temp, float &umid) {
  sensorLigar(true);
  delay(2);  // tempo de partida do SHT4x: 1 ms máx.
  Wire.begin(PINO_SDA, PINO_SCL, 100000);

  bool ok = false;
  for (int tentativa = 0; tentativa < 2 && !ok; tentativa++) {
    Wire.beginTransmission(SHT4X_ENDERECO);
    Wire.write(0xFD);  // medição de alta precisão (8,3 ms máx.)
    if (Wire.endTransmission() != 0) continue;
    delay(9);
    uint8_t d[6];
    if (Wire.requestFrom((uint8_t)SHT4X_ENDERECO, (uint8_t)6) != 6) continue;
    for (int i = 0; i < 6; i++) d[i] = Wire.read();
    if (crc8(d, 2) != d[2] || crc8(d + 3, 2) != d[5]) continue;
    uint16_t rt = (d[0] << 8) | d[1];
    uint16_t rh = (d[3] << 8) | d[4];
    temp = -45.0f + 175.0f * rt / 65535.0f;
    umid = constrain(-6.0f + 125.0f * rh / 65535.0f, 0.0f, 100.0f);
    ok = true;
  }

  Wire.end();
  pinMode(PINO_SDA, INPUT);
  pinMode(PINO_SCL, INPUT);
  sensorLigar(false);
  return ok;
}

static uint16_t lerBateriaMv() {
  uint32_t soma = 0;
  for (int i = 0; i < 8; i++) soma += analogReadMilliVolts(PINO_VBAT);
  return (uint16_t)((soma / 8) * VBAT_FATOR);
}

// --------------------------------------------------------------------------
// Buffer de leituras (circular, na memória RTC)
// --------------------------------------------------------------------------
static void guardar(const Leitura &l) {
  if (st.n == BUFFER_MAX) {  // rede fora por muito tempo: descarta a mais antiga
    st.inicio = (st.inicio + 1) % BUFFER_MAX;
    st.n--;
    st.perdidas++;
  }
  st.buf[(st.inicio + st.n) % BUFFER_MAX] = l;
  st.n++;
}

static void avaliarAlarme(const Leitura &l) {
  float t = l.temp_c100 / 100.0f;
  bool erro = l.flags & FLAG_ERRO_SENSOR;
  bool fora = erro || t < TEMP_MIN || t > TEMP_MAX;
  bool dentro = !erro && t >= TEMP_MIN + TEMP_HISTERESE && t <= TEMP_MAX - TEMP_HISTERESE;

  if (fora) {
    if (st.seq_fora < 255) st.seq_fora++;
  } else if (dentro) {
    st.seq_fora = 0;
  }

  if (!st.em_alarme && st.seq_fora >= LEITURAS_ALARME) {
    st.em_alarme = true;
    st.alarme_pendente = true;
  } else if (st.em_alarme && dentro) {
    st.em_alarme = false;
    st.alarme_pendente = true;
  }
}

// --------------------------------------------------------------------------
// Wi-Fi: conexão rápida (IP fixo + canal/BSSID lembrados) e envio HTTP enxuto
// --------------------------------------------------------------------------
static bool conectarWiFi() {
  WiFi.persistent(false);  // não grava credenciais na flash a cada boot
  WiFi.mode(WIFI_STA);
#if USAR_IP_FIXO
  WiFi.config(IPAddress(IP_FIXO), IPAddress(IP_GATEWAY), IPAddress(IP_MASCARA), IPAddress(IP_DNS));
#endif
  WiFi.setTxPower(WIFI_POTENCIA);

  uint32_t timeout;
  if (st.wifi_cache) {
    WiFi.begin(WIFI_SSID, WIFI_SENHA, st.canal, st.bssid, true);
    timeout = WIFI_TIMEOUT_RAPIDO_MS;
  } else {
    WiFi.begin(WIFI_SSID, WIFI_SENHA);
    timeout = WIFI_TIMEOUT_COMPLETO_MS;
  }

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeout) delay(5);
  if (WiFi.status() != WL_CONNECTED) {
    st.wifi_cache = false;  // roteador pode ter mudado de canal: próxima vez faz varredura
    return false;
  }

  st.canal = WiFi.channel();
  memcpy(st.bssid, WiFi.BSSID(), 6);
  st.wifi_cache = true;
  LOG("Wi-Fi conectado em %lu ms (canal %u)\n", millis() - t0, st.canal);
  return true;
}

static String montarJson(uint32_t agora, uint16_t bat_mv, int rssi) {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char cab[320];
  snprintf(cab, sizeof(cab),
           "{\"id\":\"%02X%02X%02X%02X%02X%02X\",\"fw\":\"%s\",\"boot\":%lu,\"seq\":%lu,"
           "\"agora\":%lu,\"bat_mv\":%u,\"rssi\":%d,\"alarme\":%s,\"perdidas\":%u,"
           "\"tmin\":%.1f,\"tmax\":%.1f,\"intervalo_envio_s\":%u,\"leituras\":[",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], FIRMWARE_VERSAO,
           (unsigned long)st.boot, (unsigned long)st.seq, (unsigned long)agora, bat_mv, rssi,
           st.em_alarme ? "true" : "false", st.perdidas, TEMP_MIN, TEMP_MAX,
           (unsigned)(INTERVALO_MEDICAO_S * LEITURAS_POR_ENVIO));

  String json;
  json.reserve(strlen(cab) + st.n * 26 + 4);
  json += cab;
  char item[40];
  for (uint16_t i = 0; i < st.n; i++) {
    const Leitura &l = st.buf[(st.inicio + i) % BUFFER_MAX];
    snprintf(item, sizeof(item), "%s[%lu,%d,%u,%u]", i ? "," : "", (unsigned long)l.t,
             l.temp_c100, l.umid, l.flags);
    json += item;
  }
  json += "]}";
  return json;
}

static bool enviarHTTP(const String &json) {
  WiFiClient cli;
  IPAddress ip;
  bool conectou = ip.fromString(SERVIDOR_HOST)
                      ? cli.connect(ip, SERVIDOR_PORTA, HTTP_TIMEOUT_MS)
                      : cli.connect(SERVIDOR_HOST, SERVIDOR_PORTA, HTTP_TIMEOUT_MS);
  if (!conectou) return false;

  cli.printf("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
             "X-Token: %s\r\nContent-Length: %u\r\nConnection: close\r\n\r\n",
             SERVIDOR_CAMINHO, SERVIDOR_HOST, SERVIDOR_TOKEN, json.length());
  cli.print(json);

  uint32_t t0 = millis();
  while (!cli.available() && cli.connected() && millis() - t0 < HTTP_TIMEOUT_MS) delay(2);
  String status = cli.readStringUntil('\n');  // "HTTP/1.1 200 OK"
  cli.stop();
  LOG("Resposta: %s\n", status.c_str());
  return status.startsWith("HTTP/1.") && status.charAt(9) == '2';
}

static bool enviarLote(uint32_t agora) {
  uint16_t bat = lerBateriaMv();
  if (!conectarWiFi()) return false;
  st.seq++;
  bool ok = enviarHTTP(montarJson(agora, bat, WiFi.RSSI()));
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  return ok;
}

// --------------------------------------------------------------------------
// Ciclo principal: tudo acontece no setup(); loop() nunca é executado.
// --------------------------------------------------------------------------
void setup() {
#ifdef DEPURACAO
  Serial.begin(115200);
  delay(1500);
#endif
  setCpuFrequencyMhz(80);  // suficiente para medir; o Wi-Fi funciona normalmente a 80 MHz

  bool acordou_timer = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER;
  bool primeira_vez = st.magico != MAGICO || !acordou_timer;
  if (st.magico != MAGICO) {
    memset(&st, 0, sizeof(st));
    st.magico = MAGICO;
    st.boot = esp_random();
  }

  uint32_t agora = agoraS();

  Leitura l = {agora, 0, 0, 0};
  float temp, umid;
  if (lerSHT4x(temp, umid)) {
    l.temp_c100 = (int16_t)lroundf(temp * 100.0f);
    l.umid = (uint8_t)lroundf(umid);
    if (temp < TEMP_MIN || temp > TEMP_MAX) l.flags |= FLAG_FORA_FAIXA;
  } else {
    l.flags |= FLAG_ERRO_SENSOR;
  }
  guardar(l);
  avaliarAlarme(l);
  LOG("T=%.2f UR=%u flags=%u n=%u alarme=%d\n", l.temp_c100 / 100.0, l.umid, l.flags, st.n,
      st.em_alarme);

  // Envia só quando vale a pena: lote cheio, mudança de alarme, alarme ativo ou instalação.
  // Mudança de alarme ignora a espera de rede, mas só nas 3 primeiras falhas.
  bool alarme_ativo = st.em_alarme && (int32_t)(agora - st.ultimo_envio) >= ENVIO_ALARME_S;
  bool enviar = primeira_vez || st.alarme_pendente || alarme_ativo || st.n >= LEITURAS_POR_ENVIO;
  bool urgente = (primeira_vez || st.alarme_pendente) && st.falhas < 3;
  if (enviar && (urgente || (int32_t)(agora - st.proxima_tentativa) >= 0)) {
    if (enviarLote(agora)) {
      st.inicio = 0;
      st.n = 0;
      st.perdidas = 0;
      st.alarme_pendente = false;
      st.falhas = 0;
      st.proxima_tentativa = 0;
      st.ultimo_envio = agora;
    } else {
      // Rede fora: tenta de novo com espera crescente para não drenar a bateria.
      if (st.falhas < 16) st.falhas++;
      uint32_t espera = (uint32_t)INTERVALO_MEDICAO_S << min<uint8_t>(st.falhas - 1, 6);
      st.proxima_tentativa = agora + min<uint32_t>(espera, BACKOFF_MAX_S);
    }
  }

  uint32_t intervalo = st.em_alarme ? INTERVALO_ALARME_S : INTERVALO_MEDICAO_S;
  uint64_t acordado_us = (uint64_t)millis() * 1000ULL;
  uint64_t dormir_us = (uint64_t)intervalo * 1000000ULL;
  dormir_us = dormir_us > acordado_us + 1000000ULL ? dormir_us - acordado_us : 1000000ULL;

  LOG("Acordado %lu ms, dormindo %lu s\n", millis(), (unsigned long)(dormir_us / 1000000ULL));
#ifdef DEPURACAO
  Serial.flush();
#endif
  gpio_deep_sleep_hold_en();
  esp_sleep_enable_timer_wakeup(dormir_us);
  esp_deep_sleep_start();
}

void loop() {}
