/*
 * GreenFlow / Flowincheck — ESP32-C6 traffic-light controller
 * -----------------------------------------------------------
 * Menjalankan siklus MERAH -> HIJAU -> KUNING dengan durasi dari server:
 *   - HTTP  : GET /api/light/latest  (seperti sebelumnya, selalu jalan sebagai dasar)
 *   - MQTT  : subscribe <prefix>/<slug>/status  -> program baru langsung diterima
 * Program baru hanya dipakai di awal siklus berikutnya, jadi lampu tidak pernah
 * melompat di tengah fase.
 *
 * Opsi yang bisa diubah dari tab Admin (konfigurasi lewat MQTT atau HTTP):
 *   poll_interval_s, fallback_red/yellow/green, offline_mode, offline_after_s
 *   offline_mode (dipakai kalau server tidak terjangkau lebih dari offline_after_s):
 *     fallback     -> jalankan program fallback_* (default)
 *     blink_yellow -> kuning berkedip (tanda hati-hati)
 *     all_red      -> merah terus
 * Perintah dari dashboard: reboot.
 *
 * Yang TIDAK bisa diubah dari dashboard (butuh flash ulang): WiFi, alamat server,
 * kredensial MQTT, nomor pin.
 *
 * Wiring: RED -> GPIO 4, YELLOW -> GPIO 5, GREEN -> GPIO 6, GND bersama.
 * Modul LED 5V jangan diberi daya dari pin 3V3 ESP32.
 *
 * Library: PubSubClient, ArduinoJson (>= 6.21). Board: ESP32C6 Dev Module (core >= 3.0).
 * STATUS: belum dikompilasi/diuji di perangkat oleh pembuat patch ini.
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ---------------------- USER CONFIG ----------------------------------
const char* WIFI_SSID     = "ISI_NAMA_WIFI";
const char* WIFI_PASSWORD = "ISI_PASSWORD_WIFI";

const char* SERVER_HOST   = "your-vps-domain-or-ip";
const int   SERVER_PORT   = 8000;
const char* LOCATION_NAME = "Malang-ESP32";   // sama persis dengan config.yaml & firmware kamera
const char* LOCATION_SLUG = "malang-esp32";   // huruf kecil, selain a-z0-9 jadi "-"

const bool  MQTT_ENABLED  = true;
const char* MQTT_BROKER   = "IP_VPS_KAMU";
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "greenflow";
const char* MQTT_PASS     = "";
const char* MQTT_PREFIX   = "greenflow";

const char* FW_VERSION    = "2.1.0";

const int PIN_RED    = 4;
const int PIN_YELLOW = 5;
const int PIN_GREEN  = 6;
// -----------------------------------------------------------------------

enum OfflineMode { OFFLINE_FALLBACK, OFFLINE_BLINK_YELLOW, OFFLINE_ALL_RED };

struct LightConfig {
  uint32_t pollMs = 15000;
  uint32_t fallbackRed = 180, fallbackYellow = 30, fallbackGreen = 420;
  OfflineMode offlineMode = OFFLINE_FALLBACK;
  uint32_t offlineAfterMs = 120000;
  uint32_t version = 0;
} cfg;

// Program aktif & program berikutnya (baseline dari server saat belum pernah terhubung)
uint32_t redSec = 180, yellowSec = 30, greenSec = 420;
uint32_t pendingRed = 180, pendingYellow = 30, pendingGreen = 420;

uint32_t lastPollMs = 0, lastServerOkMs = 0, lastStateMs = 0, lastMqttTryMs = 0;
bool everReachedServer = false;
bool inOffline = false;

String deviceId;
WiFiClient netClient;
PubSubClient mqtt(netClient);
char topicConfig[96], topicCmd[96], topicStatus[96], topicState[128];

// ------------------------------------------------------------ util -------

String urlEncode(const char* s) {
  String out;
  for (; *s; s++) {
    char c = *s;
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += c;
    else { char buf[4]; snprintf(buf, sizeof(buf), "%%%02X", (uint8_t)c); out += buf; }
  }
  return out;
}

void setLights(bool r, bool y, bool g) {
  digitalWrite(PIN_RED, r ? HIGH : LOW);
  digitalWrite(PIN_YELLOW, y ? HIGH : LOW);
  digitalWrite(PIN_GREEN, g ? HIGH : LOW);
}

// True kalau tidak ada kabar dari server (HTTP maupun MQTT) lebih dari offline_after_s
bool serverOffline() {
  if (!everReachedServer) return millis() > cfg.offlineAfterMs;   // belum pernah terhubung sejak boot
  return millis() - lastServerOkMs > cfg.offlineAfterMs;
}

void markServerOk() {
  everReachedServer = true;
  lastServerOkMs = millis();
}

// ------------------------------------------------------ konfigurasi ------

void applyConfig(JsonVariantConst root) {
  uint32_t version = root["version"] | 0;
  JsonObjectConst l = root["light"];
  if (l.isNull()) return;
  if (version != 0 && version == cfg.version) return;

  cfg.pollMs         = constrain((int)(l["poll_interval_s"] | (int)(cfg.pollMs / 1000)), 5, 300) * 1000UL;
  cfg.fallbackRed    = constrain((int)(l["fallback_red"]    | (int)cfg.fallbackRed), 1, 3600);
  cfg.fallbackYellow = constrain((int)(l["fallback_yellow"] | (int)cfg.fallbackYellow), 3, 600);   // kuning min 3 detik
  cfg.fallbackGreen  = constrain((int)(l["fallback_green"]  | (int)cfg.fallbackGreen), 1, 3600);
  cfg.offlineAfterMs = constrain((int)(l["offline_after_s"] | (int)(cfg.offlineAfterMs / 1000)), 10, 3600) * 1000UL;

  const char* mode = l["offline_mode"] | "fallback";
  if (!strcmp(mode, "blink_yellow")) cfg.offlineMode = OFFLINE_BLINK_YELLOW;
  else if (!strcmp(mode, "all_red")) cfg.offlineMode = OFFLINE_ALL_RED;
  else cfg.offlineMode = OFFLINE_FALLBACK;

  cfg.version = version;
  lastStateMs = 0;      // laporkan versi baru ke dashboard
  Serial.printf("[config] v%u poll=%ums offline=%d after=%ums\n", cfg.version, cfg.pollMs, (int)cfg.offlineMode, cfg.offlineAfterMs);
}

bool fetchConfigHttp() {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT +
               "/api/device/config?role=light&device_id=" + deviceId +
               "&location=" + urlEncode(LOCATION_NAME) +
               "&fw=" + FW_VERSION + "&cv=" + String(cfg.version) + "&rssi=" + String(WiFi.RSSI());
  http.begin(url);
  http.setTimeout(6000);
  int code = http.GET();
  if (code != 200) { http.end(); return false; }
  String body = http.getString();
  http.end();
  StaticJsonDocument<768> doc;
  if (deserializeJson(doc, body)) return false;
  applyConfig(doc.as<JsonVariantConst>());
  return true;
}

// ------------------------------------------------------------ WiFi -------

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[wifi] menyambung ke %s", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) { delay(400); Serial.print("."); }
  Serial.println(WiFi.status() == WL_CONNECTED ? "\n[wifi] tersambung" : "\n[wifi] timeout, akan dicoba lagi");
}

// ------------------------------------------------------------ program ----

// Program baru disimpan di pending*, baru dipakai di awal siklus berikutnya
void setPending(uint32_t r, uint32_t y, uint32_t g) {
  if (r == 0 || y == 0 || g == 0) return;
  pendingRed = r; pendingYellow = (y < 3 ? 3 : y); pendingGreen = g;
}

bool fetchLatestProgram() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) return false;
  }
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT +
               "/api/light/latest?location=" + urlEncode(LOCATION_NAME);
  http.begin(url);
  http.setTimeout(6000);
  int code = http.GET();
  if (code != 200) { Serial.printf("[poll] HTTP %d\n", code); http.end(); return false; }
  String body = http.getString();
  http.end();

  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, body)) return false;
  setPending(doc["red_seconds"] | 0, doc["yellow_seconds"] | 0, doc["green_seconds"] | 0);
  markServerOk();
  Serial.printf("[poll] program -> R:%u Y:%u G:%u (%s)\n", pendingRed, pendingYellow, pendingGreen,
                (const char*)(doc["traffic_status"] | "?"));
  return true;
}

// ------------------------------------------------------------ MQTT -------

void publishState(bool online) {
  if (!mqtt.connected()) return;
  StaticJsonDocument<256> doc;
  doc["online"] = online;
  doc["role"] = "light";
  doc["rssi"] = WiFi.RSSI();
  doc["fw"] = FW_VERSION;
  doc["config_version"] = cfg.version;
  doc["uptime_s"] = millis() / 1000;
  char buf[256];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(topicState, (const uint8_t*)buf, n, true);
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<768> doc;
  if (deserializeJson(doc, payload, length)) return;

  if (!strcmp(topic, topicConfig)) {
    applyConfig(doc.as<JsonVariantConst>());
  } else if (!strcmp(topic, topicStatus)) {
    // hasil deteksi terbaru dari server -> program lampu langsung diterima
    setPending(doc["red_seconds"] | 0, doc["yellow_seconds"] | 0, doc["green_seconds"] | 0);
    markServerOk();
  } else if (!strcmp(topic, topicCmd)) {
    const char* target = doc["target"] | "all";
    if (strcmp(target, "all") && strcmp(target, "light")) return;
    if (!strcmp(doc["cmd"] | "", "reboot")) {
      publishState(false);
      mqtt.loop();
      delay(300);
      ESP.restart();
    }
  }
}

void setupMqtt() {
  if (!MQTT_ENABLED) return;
  snprintf(topicConfig, sizeof(topicConfig), "%s/%s/config", MQTT_PREFIX, LOCATION_SLUG);
  snprintf(topicCmd, sizeof(topicCmd), "%s/%s/cmd", MQTT_PREFIX, LOCATION_SLUG);
  snprintf(topicStatus, sizeof(topicStatus), "%s/%s/status", MQTT_PREFIX, LOCATION_SLUG);
  snprintf(topicState, sizeof(topicState), "%s/%s/device/%s/state", MQTT_PREFIX, LOCATION_SLUG, deviceId.c_str());
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(1024);
  mqtt.setCallback(onMqttMessage);
}

void maintainMqtt() {
  if (!MQTT_ENABLED || WiFi.status() != WL_CONNECTED) return;
  if (!mqtt.connected()) {
    if (millis() - lastMqttTryMs < 5000) return;
    lastMqttTryMs = millis();
    String clientId = String("gf-light-") + deviceId;
    const char* will = "{\"online\":false,\"role\":\"light\"}";
    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS, topicState, 1, true, will)) {
      mqtt.subscribe(topicConfig, 1);
      mqtt.subscribe(topicStatus, 1);
      mqtt.subscribe(topicCmd, 0);
      publishState(true);
      Serial.println("[mqtt] tersambung");
    }
    return;
  }
  mqtt.loop();
  if (lastStateMs == 0 || millis() - lastStateMs > 30000UL) {
    publishState(true);
    lastStateMs = millis();
  }
}

// ------------------------------------------------------------ siklus -----

// Dipanggil terus selama lampu menahan satu fase
void maintain() {
  maintainMqtt();
  if (millis() - lastPollMs >= cfg.pollMs) {
    fetchLatestProgram();
    if (!mqtt.connected()) fetchConfigHttp();     // MQTT mati -> konfigurasi lewat HTTP
    lastPollMs = millis();
  }
}

void holdPhase(uint32_t seconds, bool abortOnOffline = true) {
  uint32_t end = millis() + seconds * 1000UL;
  while ((int32_t)(end - millis()) > 0) {
    maintain();
    // server hilang terlalu lama dan mode bukan fallback: berhenti menahan fase, loop() mengambil alih
    if (abortOnOffline && serverOffline() && cfg.offlineMode != OFFLINE_FALLBACK) return;
    delay(100);
  }
}

// Satu "irisan" 1 detik pola offline (dipanggil berulang dari loop)
void runOfflineSlice() {
  if (cfg.offlineMode == OFFLINE_ALL_RED) {
    setLights(true, false, false);
    for (int i = 0; i < 10; i++) { maintain(); delay(100); }
  } else {   // OFFLINE_BLINK_YELLOW
    setLights(false, true, false);
    for (int i = 0; i < 5; i++) { maintain(); delay(100); }
    setLights(false, false, false);
    for (int i = 0; i < 5; i++) { maintain(); delay(100); }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_YELLOW, OUTPUT);
  pinMode(PIN_GREEN, OUTPUT);
  setLights(true, false, false);               // aman: mulai dari merah

  deviceId = String("light-") + LOCATION_SLUG; // sama dengan nama yang dipakai server
  connectWiFi();
  setupMqtt();
  fetchConfigHttp();
  fetchLatestProgram();
  lastPollMs = millis();
}

void loop() {
  maintain();

  if (serverOffline()) {
    if (cfg.offlineMode == OFFLINE_FALLBACK) {
      setPending(cfg.fallbackRed, cfg.fallbackYellow, cfg.fallbackGreen);
      inOffline = false;
    } else {
      if (!inOffline) {
        inOffline = true;
        // Jangan memotong hijau langsung ke merah: kuning dulu supaya aman bagi pengendara
        if (digitalRead(PIN_GREEN) == HIGH) {
          setLights(false, true, false);
          holdPhase(cfg.fallbackYellow, false);
        }
      }
      runOfflineSlice();
      return;                                   // cek ulang kondisi server tiap detik
    }
  } else {
    inOffline = false;
  }

  // Awal siklus baru: pakai program terakhir yang diterima
  redSec = pendingRed;
  yellowSec = pendingYellow;
  greenSec = pendingGreen;

  setLights(true, false, false);
  holdPhase(redSec);
  if (serverOffline() && cfg.offlineMode != OFFLINE_FALLBACK) return;

  setLights(false, false, true);
  holdPhase(greenSec);
  if (serverOffline() && cfg.offlineMode != OFFLINE_FALLBACK) return;

  setLights(false, true, false);
  holdPhase(yellowSec);
}
