// Configuração do sensor. Ajuste conforme o equipamento monitorado.
#pragma once

#if __has_include("segredos.h")
#include "segredos.h"
#else
#warning "segredos.h nao encontrado: usando segredos.exemplo.h"
#include "segredos.exemplo.h"
#endif

// ---------------------------------------------------------------------------
// Rede
// ---------------------------------------------------------------------------
// IP fixo evita o DHCP (economiza ~0,5 a 2 s de rádio ligado em cada envio).
#define USAR_IP_FIXO 1
#define IP_FIXO      192, 168, 1, 50
#define IP_GATEWAY   192, 168, 1, 1
#define IP_MASCARA   255, 255, 255, 0
#define IP_DNS       192, 168, 1, 1

// Potência de transmissão. Menor = menos pico de corrente; aumente se o sinal for fraco.
// Valores válidos: WIFI_POWER_19_5dBm, WIFI_POWER_15dBm, WIFI_POWER_11dBm, WIFI_POWER_8_5dBm ...
#define WIFI_POTENCIA WIFI_POWER_15dBm

#define WIFI_TIMEOUT_RAPIDO_MS   3000   // reconexão usando canal/BSSID em cache
#define WIFI_TIMEOUT_COMPLETO_MS 10000  // conexão com varredura completa
#define HTTP_TIMEOUT_MS          3000
#define SERVIDOR_CAMINHO         "/api/leituras"

// ---------------------------------------------------------------------------
// Temporização (o maior impacto na duração da bateria)
// ---------------------------------------------------------------------------
#define INTERVALO_MEDICAO_S  300   // mede a cada 5 min (o rádio fica desligado)
#define LEITURAS_POR_ENVIO   12    // envia em lote a cada 12 medições = 1 h
#define INTERVALO_ALARME_S   120   // em alarme, mede com mais frequência
#define ENVIO_ALARME_S       600   // em alarme, reenvia a situação a cada 10 min
#define BACKOFF_MAX_S        3600  // espera máxima entre tentativas se a rede cair
#define BUFFER_MAX           288   // 24 h de leituras guardadas na RTC se a rede cair

// ---------------------------------------------------------------------------
// Faixa de temperatura aceitável (°C)
// Refrigerador: 0 a 5 | Freezer / câmara de congelados: -25 a -15 | Vacinas: 2 a 8
// ---------------------------------------------------------------------------
#define TEMP_MIN         0.0f
#define TEMP_MAX         5.0f
#define TEMP_HISTERESE   0.5f   // para sair do alarme, precisa voltar 0,5 °C para dentro
#define LEITURAS_ALARME  3      // N leituras seguidas fora da faixa (evita alarme ao abrir a porta)

// ---------------------------------------------------------------------------
// Hardware (ESP32-C3)
// ---------------------------------------------------------------------------
#define PINO_SDA          6
#define PINO_SCL          7
#define PINO_SENSOR_VCC   3     // alimenta o SHT4x e os pull-ups do I2C só durante a leitura
#define PINO_VBAT         2     // ADC1: meio do divisor 4,7 MΩ / 2,2 MΩ da bateria
#define VBAT_FATOR        ((4.7f + 2.2f) / 2.2f)
#define SHT4X_ENDERECO    0x44

#define FIRMWARE_VERSAO "1.0.0"
