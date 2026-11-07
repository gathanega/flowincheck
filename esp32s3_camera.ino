/*
 * GreenFlow / Flowincheck — ESP32-S3 camera node
 * ----------------------------------------------
 * Mengirim frame JPEG ke server (HTTP POST) seperti sebelumnya, ditambah:
 *   - opsi kamera yang bisa diubah dari tab Admin dashboard
 *       (framesize, jpeg_quality, interval kirim, vflip, hmirror, brightness, contrast, saturation)
 *   - konfigurasi diambil lewat MQTT (instan, retained) dan/atau HTTP (cadangan)
 *   - status perangkat (online/offline, RSSI, versi, versi konfigurasi) lewat MQTT
 *   - perintah dari dashboard: capture_now, reboot
 *
 * Yang TIDAK bisa diubah dari dashboard (butuh flash ulang): WiFi, alamat server,
 * kredensial MQTT, pin kamera.
 *
 * Library (Library Manager): PubSubClient (Nick O'Leary), ArduinoJson (>= 6.21)
 * Board: ESP32S3 Dev Module, Espressif esp32 core >= 3.0
 *
 * STATUS: belum dikompilasi/diuji di perangkat oleh pembuat patch ini.
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ---------------------- USER CONFIG ----------------------------------
const char* WIFI_SSID     = "";
const char* WIFI_PASSWORD = "";

// Server FastAPI (tanpa http://)
const char* SERVER_HOST   = "your-vps-domain-or-ip";   // samakan dengan firmware lampu ESP32-C6
const int   SERVER_PORT   = 8000;                      // 80 kalau lewat nginx
const char* DEVICE_ID     = "esp32-malang-1";
const char* LOCATION_NAME = "Malang-ESP32";   // sama persis dengan nama lokasi di config.yaml
const char* LOCATION_SLUG = "malang-esp32";   // = huruf kecil, selain a-z0-9 jadi "-" (tampil di tab Admin)

// MQTT (opsional). Kalau false, konfigurasi hanya diambil lewat HTTP.
const bool  MQTT_ENABLED  = true;
const char* MQTT_BROKER   = "IP_VPS_KAMU";
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "greenflow";
const char* MQTT_PASS     = "";
const char* MQTT_PREFIX   = "greenflow";      // sama dengan mqtt.prefix di config.yaml

const char* FW_VERSION    = "2.1.0";
// -----------------------------------------------------------------------

// ---- OV3660 pin map (generic ESP32-S3 camera board) --------------------
// NOTE: cocokkan dengan breakout kamera yang dipakai.
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4
#define CAM_PIN_SIOC    5
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13
// -------------------------------------------------------------------------

// Konfigurasi yang bisa diubah dari dashboard (nilai awal = default server)
struct CamConfig {
  framesize_t framesize = FRAMESIZE_SVGA;
  int quality = 12;                 // 4 = terbaik ... 63 = file terkecil
  uint32_t intervalMs = 5000;
  bool vflip = true;
  bool hmirror = false;
  int brightness = 1;               // -2..2
  int contrast = 0;                 // -2..2
  int saturation = 0;               // -2..2
  uint32_t version = 0;             // versi konfigurasi dari server
} cfg;

bool hasPsram = false;
bool forceCapture = false;
uint32_t lastCaptureMs = 0, lastStateMs = 0, lastCfgPollMs = 0, lastMqttTryMs = 0;

WiFiClient netClient;
PubSubClient mqtt(netClient);
char topicConfig[96], topicCmd[96], topicState[128];

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

bool parseFramesize(const char* name, framesize_t &out) {
  if (!name) return false;
  if (!strcmp(name, "QQVGA")) out = FRAMESIZE_QQVGA;       // 160x120
  else if (!strcmp(name, "QVGA"))  out = FRAMESIZE_QVGA;   // 320x240
  else if (!strcmp(name, "VGA"))   out = FRAMESIZE_VGA;    // 640x480
  else if (!strcmp(name, "SVGA"))  out = FRAMESIZE_SVGA;   // 800x600
  else if (!strcmp(name, "XGA"))   out = FRAMESIZE_XGA;    // 1024x768
  else if (!strcmp(name, "SXGA"))  out = FRAMESIZE_SXGA;   // 1280x1024
  else if (!strcmp(name, "UXGA"))  out = FRAMESIZE_UXGA;   // 1600x1200
  else return false;
  return true;
}

// Tanpa PSRAM, frame besar gagal dialokasikan -> paksa maksimal QVGA
framesize_t safeFramesize(framesize_t fs) {
  return (!hasPsram && fs > FRAMESIZE_QVGA) ? FRAMESIZE_QVGA : fs;
}

// ---------------------------------------------------------- kamera -------

void applySensorSettings() {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return;
  s->set_framesize(s, safeFramesize(cfg.framesize));
  s->set_quality(s, cfg.quality);
  s->set_vflip(s, cfg.vflip ? 1 : 0);
  s->set_hmirror(s, cfg.hmirror ? 1 : 0);
  s->set_brightness(s, cfg.brightness);
  s->set_contrast(s, cfg.contrast);
  s->set_saturation(s, cfg.saturation);
}

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = CAM_PIN_D0; config.pin_d1 = CAM_PIN_D1;
  config.pin_d2 = CAM_PIN_D2; config.pin_d3 = CAM_PIN_D3;
  config.pin_d4 = CAM_PIN_D4; config.pin_d5 = CAM_PIN_D5;
  config.pin_d6 = CAM_PIN_D6; config.pin_d7 = CAM_PIN_D7;
  config.pin_xclk = CAM_PIN_XCLK;
  config.pin_pclk = CAM_PIN_PCLK;
  config.pin_vsync = CAM_PIN_VSYNC;
  config.pin_href = CAM_PIN_HREF;
  config.pin_sccb_sda = CAM_PIN_SIOD;
  config.pin_sccb_scl = CAM_PIN_SIOC;
  config.pin_pwdn = CAM_PIN_PWDN;
  config.pin_reset = CAM_PIN_RESET;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  hasPsram = psramFound();
  Serial.printf("[camera] PSRAM: %s\n", hasPsram ? "ada" : "tidak ada");

  config.frame_size = safeFramesize(cfg.framesize);
  config.jpeg_quality = cfg.quality;
  if (hasPsram) {
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_LATEST;
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.fb_count = 1;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[camera] init gagal: 0x%x\n", err);
    return false;
  }
  applySensorSettings();
  return true;
}

// ------------------------------------------------------ konfigurasi ------

// Dipanggil dari MQTT dan HTTP. `root` = {"version":n,"camera":{...}}
void applyConfig(JsonVariantConst root) {
  uint32_t version = root["version"] | 0;
  JsonObjectConst cam = root["camera"];
  if (cam.isNull()) return;
  if (version != 0 && version == cfg.version) return;      // sudah terpasang

  framesize_t fs;
  if (parseFramesize(cam["framesize"] | "", fs)) cfg.framesize = fs;
  cfg.quality    = constrain((int)(cam["jpeg_quality"] | cfg.quality), 4, 63);
  cfg.intervalMs = constrain((int)(cam["capture_interval_s"] | (int)(cfg.intervalMs / 1000)), 1, 600) * 1000UL;
  cfg.vflip      = cam["vflip"]  | cfg.vflip;
  cfg.hmirror    = cam["hmirror"] | cfg.hmirror;
  cfg.brightness = constrain((int)(cam["brightness"] | cfg.brightness), -2, 2);
  cfg.contrast   = constrain((int)(cam["contrast"] | cfg.contrast), -2, 2);
  cfg.saturation = constrain((int)(cam["saturation"] | cfg.saturation), -2, 2);
  cfg.version    = version;

  applySensorSettings();
  Serial.printf("[config] v%u: fs=%d q=%d interval=%ums\n", cfg.version, (int)cfg.framesize, cfg.quality, cfg.intervalMs);
  lastStateMs = 0;      // laporkan versi baru ke dashboard segera
}

// Cadangan kalau MQTT mati: GET /api/device/config
bool fetchConfigHttp() {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT +
               "/api/device/config?role=camera&device_id=" + DEVICE_ID +
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
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) { delay(400); Serial.print("."); }
  Serial.println(WiFi.status() == WL_CONNECTED ? "\n[wifi] tersambung" : "\n[wifi] timeout, coba lagi nanti");
}

// ------------------------------------------------------------ MQTT -------

void publishState(bool online) {
  if (!mqtt.connected()) return;
  StaticJsonDocument<256> doc;
  doc["online"] = online;
  doc["role"] = "camera";
  doc["rssi"] = WiFi.RSSI();
  doc["fw"] = FW_VERSION;
  doc["config_version"] = cfg.version;
  doc["uptime_s"] = millis() / 1000;
  char buf[256];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(topicState, (const uint8_t*)buf, n, true);   // retained
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<768> doc;
  if (deserializeJson(doc, payload, length)) return;

  if (!strcmp(topic, topicConfig)) {
    applyConfig(doc.as<JsonVariantConst>());
  } else if (!strcmp(topic, topicCmd)) {
    const char* target = doc["target"] | "all";
    if (strcmp(target, "all") && strcmp(target, "camera")) return;   // bukan untuk kamera
    const char* cmd = doc["cmd"] | "";
    if (!strcmp(cmd, "capture_now")) {
      forceCapture = true;
    } else if (!strcmp(cmd, "reboot")) {
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
  snprintf(topicState, sizeof(topicState), "%s/%s/device/%s/state", MQTT_PREFIX, LOCATION_SLUG, DEVICE_ID);
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(1024);
  mqtt.setCallback(onMqttMessage);
}

void maintainMqtt() {
  if (!MQTT_ENABLED || WiFi.status() != WL_CONNECTED) return;
  if (!mqtt.connected()) {
    if (millis() - lastMqttTryMs < 5000) return;          // jangan memblokir loop terus-menerus
    lastMqttTryMs = millis();
    String clientId = String("gf-cam-") + DEVICE_ID;
    // Last Will: broker menandai perangkat offline kalau koneksi putus mendadak
    const char* will = "{\"online\":false,\"role\":\"camera\"}";
    if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS, topicState, 1, true, will)) {
      mqtt.subscribe(topicConfig, 1);
      mqtt.subscribe(topicCmd, 0);
      publishState(true);
      Serial.println("[mqtt] tersambung");
    } else {
      Serial.printf("[mqtt] gagal, rc=%d\n", mqtt.state());
    }
    return;
  }
  mqtt.loop();
  if (millis() - lastStateMs > 30000UL || lastStateMs == 0) {
    publishState(true);
    lastStateMs = millis();
  }
}

// ------------------------------------------------------------ kirim ------

bool sendFrame(camera_fb_t* fb) {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) return false;
  }
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT +
               "/api/ingest/esp32/" + DEVICE_ID + "?location=" + urlEncode(LOCATION_NAME);
  http.begin(url);
  http.addHeader("Content-Type", "image/jpeg");
  http.setTimeout(8000);
  int code = http.POST(fb->buf, fb->len);
  if (code > 0) Serial.printf("[send] HTTP %d, %u bytes\n", code, fb->len);
  else Serial.printf("[send] gagal: %s\n", http.errorToString(code).c_str());
  http.end();
  return code == 200;
}

void captureAndSend() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[camera] gagal mengambil frame");
    return;
  }
  sendFrame(fb);
  esp_camera_fb_return(fb);
}

// ------------------------------------------------------------ setup ------

void setup() {
  Serial.begin(115200);
  delay(300);

  if (!initCamera()) {
    Serial.println("[fatal] kamera gagal init, berhenti");
    while (true) delay(1000);
  }
  connectWiFi();
  setupMqtt();
  fetchConfigHttp();            // ambil konfigurasi terakhir dari server saat boot
  lastCfgPollMs = millis();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  maintainMqtt();

  uint32_t now = millis();
  if (forceCapture || now - lastCaptureMs >= cfg.intervalMs) {
    forceCapture = false;
    captureAndSend();
    lastCaptureMs = millis();
  }

  // HTTP hanya dipakai kalau MQTT mati/tidak tersambung
  if ((!MQTT_ENABLED || !mqtt.connected()) && now - lastCfgPollMs > 60000UL) {
    fetchConfigHttp();
    lastCfgPollMs = now;
  }
  delay(20);
}
