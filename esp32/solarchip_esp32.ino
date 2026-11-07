/*
 * SolarChip — ESP32 Solar Telemetry Node
 * 
 * SENSORS:
 *   1. Adafruit INA219 (I2C): High-side DC voltage and current monitor
 *      - SDA: GPIO 21
 *      - SCL: GPIO 22
 *      - I2C Address: 0x40 (default)
 *   2. DS18B20 1-Wire Digital Temperature Sensor: Panel surface temperature
 *      - Data: GPIO 4 (Requires 4.7kΩ pull-up resistor to 3.3V)
 *      - VDD: 3.3V
 *      - GND: Common Ground
 * 
 * SAFETY WARNING:
 *   Designed exclusively for small low-voltage educational solar panels (≤ 10 W, ≤ 21 V Voc).
 *   Never connect this experimental prototype directly to AC mains, high-voltage battery banks,
 *   or commercial rooftop PV arrays. Always verify polarity before applying power.
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_INA219.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ============================================================================
// CONFIGURATION (Adjust before flashing)
// ============================================================================

// Wi-Fi network credentials
// Note: For security, never commit real passwords to public version control!
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// SolarChip backend API URL.
// Replace '192.168.1.100' with your host computer's local LAN IP address.
// On Windows run `ipconfig`; on macOS/Linux run `ifconfig` or `ip a`.
const char* SERVER_URL    = "http://192.168.1.100:5000/api/readings";

// Telemetry interval in milliseconds (default: 15 seconds)
const unsigned long TELEMETRY_INTERVAL_MS = 15000;

// Hardware pin definitions
#define ONE_WIRE_BUS 4    // DS18B20 data line (GPIO 4)
#define I2C_SDA      21   // INA219 SDA (GPIO 21)
#define I2C_SCL      22   // INA219 SCL (GPIO 22)

// ============================================================================
// GLOBAL OBJECTS
// ============================================================================

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature panelTempSensor(&oneWire);
Adafruit_INA219 ina219;

bool ina219Ready = false;
bool tempSensorReady = false;
unsigned long lastSendMillis = 0;

// Connect or reconnect to local Wi-Fi
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.println("\n[WiFi] Connecting to: " + String(WIFI_SSID));
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 12000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WiFi] Connected!");
    Serial.print("[WiFi] IP Address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n[WiFi] Connection failed. Will retry during main loop.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n========================================");
  Serial.println("  SolarChip ESP32 Telemetry Node Boot   ");
  Serial.println("========================================");

  // Initialize I2C for INA219
  Wire.begin(I2C_SDA, I2C_SCL);
  if (!ina219.begin()) {
    Serial.println("[ERROR] INA219 not detected at I2C 0x40. Verify SDA/SCL wiring.");
    ina219Ready = false;
  } else {
    Serial.println("[OK] INA219 initialized successfully.");
    ina219Ready = true;
  }

  // Initialize Dallas DS18B20 OneWire sensor
  panelTempSensor.begin();
  int deviceCount = panelTempSensor.getDeviceCount();
  if (deviceCount == 0) {
    Serial.println("[WARN] No DS18B20 sensor found on GPIO 4. Verify 4.7k pull-up resistor.");
    tempSensorReady = false;
  } else {
    Serial.printf("[OK] DS18B20 initialized (%d probe detected).\n", deviceCount);
    tempSensorReady = true;
  }

  // Connect to Wi-Fi
  connectWiFi();
}

void loop() {
  unsigned long currentMillis = millis();

  // Non-blocking timer check for telemetry transmission
  if (currentMillis - lastSendMillis >= TELEMETRY_INTERVAL_MS || lastSendMillis == 0) {
    lastSendMillis = currentMillis;

    // Ensure Wi-Fi is connected
    if (WiFi.status() != WL_CONNECTED) {
      connectWiFi();
      if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WARN] Wi-Fi offline. Skipping telemetry transmission.");
        return;
      }
    }

    // Read voltage and current from INA219
    float voltageV = 0.0;
    float currentA = 0.0;
    float powerW = 0.0;

    if (ina219Ready) {
      float busVoltage = ina219.getBusVoltage_V();
      float shuntVoltage = ina219.getShuntVoltage_mV() / 1000.0;
      voltageV = busVoltage + shuntVoltage;
      currentA = ina219.getCurrent_mA() / 1000.0;
      // Clamp negative small noise currents
      if (currentA < 0.0 && currentA > -0.01) currentA = 0.0;
      powerW = voltageV * (currentA > 0.0 ? currentA : 0.0);
    } else {
      Serial.println("[WARN] INA219 offline; attempting re-initialization...");
      if (ina219.begin()) ina219Ready = true;
    }

    // Read temperature from DS18B20
    float panelTempC = -999.0;
    panelTempSensor.requestTemperatures();
    float t = panelTempSensor.getTempCByIndex(0);
    if (t != DEVICE_DISCONNECTED_C && t > -50.0 && t < 125.0) {
      panelTempC = t;
      tempSensorReady = true;
    } else {
      tempSensorReady = false;
    }

    // Log readings locally to Serial
    Serial.println("----------------------------------------");
    Serial.printf("[READING] V: %.2f V | I: %.3f A | P: %.2f W | Temp: %s\n",
                  voltageV, currentA, powerW,
                  panelTempC > -900 ? String(panelTempC, 1) + " C" : "N/A");

    // Construct JSON payload
    String payload = "{";
    payload += "\"voltage\":" + String(voltageV, 3) + ",";
    payload += "\"current\":" + String(currentA, 3);
    if (panelTempC > -900) {
      payload += ",\"panel_temp\":" + String(panelTempC, 2);
    }
    payload += "}";

    // Transmit HTTP POST to SolarChip backend
    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(5000); // 5 second timeout

    int httpCode = http.POST(payload);

    if (httpCode > 0) {
      if (httpCode == HTTP_CODE_CREATED || httpCode == HTTP_CODE_OK) {
        Serial.printf("[HTTP %d] Telemetry successfully received by server.\n", httpCode);
      } else {
        String resp = http.getString();
        Serial.printf("[HTTP %d] Server returned: %s\n", httpCode, resp.c_str());
      }
    } else {
      Serial.printf("[HTTP ERROR] Failed to connect to %s (Error: %s)\n",
                    SERVER_URL, http.errorToString(httpCode).c_str());
    }

    http.end();
  }

  // Yield to allow background network processes
  delay(50);
}
