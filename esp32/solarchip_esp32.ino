/*
 * SolarChip — ESP32 Solar Telemetry Node
 * 
 * EXACT HARDWARE BILL OF MATERIALS:
 *   1. 6V, 5W Mini Solar Panel
 *   2. ESP32 DevKit V1 (ESP32-WROOM-32)
 *   3. INA219 High-Side I2C DC Voltage & Current Sensor Module
 *   4. DS18B20 Waterproof Temperature Sensor (Panel Surface Temp)
 *   5. DHT22 / AM2302 Temperature & Humidity Sensor (Ambient Air Temp & Humidity)
 *   6. 0.96-inch I2C OLED Display (SSD1306, 128x64)
 *   7. Breadboard & Jumper Wire Kit
 *   8. 4.7kΩ Resistor (Pull-up between DS18B20 Data & 3.3V)
 *   9. 6V, 3W DC Lamp (Testing Load)
 *  10. Micro-USB Data Cable
 * 
 * I2C PIN CONNECTIONS (Shared Bus):
 *   - ESP32 GPIO 21 -> SDA on INA219 (0x40) & SSD1306 OLED (0x3C)
 *   - ESP32 GPIO 22 -> SCL on INA219 (0x40) & SSD1306 OLED (0x3C)
 * 
 * 1-WIRE PIN CONNECTION:
 *   - ESP32 GPIO 4  -> DS18B20 Data (Yellow) with 4.7kΩ pull-up to 3.3V
 * 
 * DHT22 PIN CONNECTION:
 *   - ESP32 GPIO 18 -> DHT22 Data Pin
 * 
 * POWER CIRCUIT & LOAD:
 *   - Solar Panel (+) -> INA219 VIN+
 *   - INA219 VIN-     -> 6V 3W Lamp (+)
 *   - 6V 3W Lamp (-)  -> Solar Panel (-) & ESP32 GND (Common Ground)
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_INA219.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <DHT.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ============================================================================
// CONFIGURATION (Adjust before flashing)
// ============================================================================

// Wi-Fi network credentials (2.4 GHz only)
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// SolarChip backend API URL.
// Replace '192.168.1.100' with your host computer's local LAN IP address.
// On Windows run `ipconfig`; on macOS/Linux run `ifconfig` or `ip a`.
const char* SERVER_URL    = "http://192.168.1.100:5000/api/readings";

// Telemetry transmission interval in milliseconds (default: 15 seconds)
const unsigned long TELEMETRY_INTERVAL_MS = 15000;

// Pin Definitions
#define I2C_SDA        21   // Shared I2C Data line
#define I2C_SCL        22   // Shared I2C Clock line
#define ONE_WIRE_BUS   4    // DS18B20 Data (GPIO 4)
#define DHTPIN         18   // DHT22 Data (GPIO 18)
#define DHTTYPE        DHT22

// OLED Display parameters
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  64
#define OLED_RESET     -1
#define SCREEN_ADDRESS 0x3C // 0x3C or 0x3D for 0.96" SSD1306

// ============================================================================
// HARDWARE OBJECTS
// ============================================================================

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature panelTempSensor(&oneWire);
Adafruit_INA219 ina219;
DHT dht(DHTPIN, DHTTYPE);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

bool ina219Ready   = false;
bool panelSensorReady = false;
bool oledReady     = false;
bool dhtReady      = false;

unsigned long lastSendMillis = 0;

// ============================================================================
// DISPLAY & WIFI HELPERS
// ============================================================================

void updateOLED(float v, float i, float p, float t_panel, float t_amb, float hum, String wifiStatus) {
  if (!oledReady) return;

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Line 1: Header + WiFi status
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("SolarChip 5W | ");
  display.print(wifiStatus);

  display.drawLine(0, 10, 127, 10, SSD1306_WHITE);

  // Line 2: Voltage & Current
  display.setCursor(0, 14);
  display.printf("V: %4.2fV  I: %4.3fA", v, i);

  // Line 3: Power (Prominent)
  display.setCursor(0, 27);
  display.setTextSize(2);
  display.printf("%4.2f W", p);

  // Line 4: Temperatures & Humidity
  display.setTextSize(1);
  display.setCursor(0, 46);
  if (t_panel > -900) {
    display.printf("Panel: %4.1fC", t_panel);
  } else {
    display.print("Panel: --C");
  }

  display.setCursor(0, 56);
  if (t_amb > -900 && hum >= 0) {
    display.printf("Air: %3.1fC  H: %2.0f%%", t_amb, hum);
  } else {
    display.print("Air/H: Awaiting DHT");
  }

  display.display();
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.println("\n[WiFi] Connecting to: " + String(WIFI_SSID));
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 10000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WiFi] Connected! IP: " + WiFi.localIP().toString());
  } else {
    Serial.println("\n[WiFi] Connection failed. Main loop will retry.");
  }
}

// ============================================================================
// SETUP
// ============================================================================

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n==============================================");
  Serial.println("  SolarChip ESP32 Telemetry Node (5W Kit)    ");
  Serial.println("==============================================");

  // 1. Initialize Shared I2C Bus (SDA=21, SCL=22)
  Wire.begin(I2C_SDA, I2C_SCL);

  // 2. Initialize 0.96" OLED Display (0x3C)
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println("[WARN] SSD1306 OLED not detected at 0x3C.");
    oledReady = false;
  } else {
    oledReady = true;
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 20);
    display.println("SolarChip Starting...");
    display.println("Connecting Sensors...");
    display.display();
    Serial.println("[OK] SSD1306 128x64 OLED initialized.");
  }

  // 3. Initialize INA219 Voltage/Current Sensor (0x40)
  if (!ina219.begin()) {
    Serial.println("[ERROR] INA219 not detected at 0x40. Check I2C wiring.");
    ina219Ready = false;
  } else {
    ina219Ready = true;
    Serial.println("[OK] INA219 Voltage/Current sensor initialized.");
  }

  // 4. Initialize DS18B20 Panel Surface Temperature Sensor (GPIO 4)
  panelTempSensor.begin();
  if (panelTempSensor.getDeviceCount() == 0) {
    Serial.println("[WARN] No DS18B20 found on GPIO 4. Verify 4.7k resistor to 3.3V.");
    panelSensorReady = false;
  } else {
    panelSensorReady = true;
    Serial.println("[OK] DS18B20 panel temperature probe initialized.");
  }

  // 5. Initialize DHT22 Ambient Sensor (GPIO 18)
  dht.begin();
  dhtReady = true;
  Serial.println("[OK] DHT22 sensor initialized on GPIO 18.");

  // 6. Connect Wi-Fi
  connectWiFi();
}

// ============================================================================
// MAIN LOOP
// ============================================================================

void loop() {
  unsigned long currentMillis = millis();

  // Read sensors continuously for responsive OLED display updates
  float voltageV = 0.0;
  float currentA = 0.0;
  float powerW   = 0.0;

  if (ina219Ready) {
    float busVoltage   = ina219.getBusVoltage_V();
    float shuntVoltage = ina219.getShuntVoltage_mV() / 1000.0;
    voltageV = busVoltage + shuntVoltage;
    currentA = ina219.getCurrent_mA() / 1000.0;
    if (currentA < 0.0 && currentA > -0.01) currentA = 0.0;
    powerW = voltageV * (currentA > 0.0 ? currentA : 0.0);
  } else {
    if (ina219.begin()) ina219Ready = true;
  }

  // DS18B20 Panel surface temp
  float panelTempC = -999.0;
  panelTempSensor.requestTemperatures();
  float t_p = panelTempSensor.getTempCByIndex(0);
  if (t_p != DEVICE_DISCONNECTED_C && t_p > -40.0 && t_p < 125.0) {
    panelTempC = t_p;
    panelSensorReady = true;
  }

  // DHT22 Ambient air temp and humidity
  float ambTempC  = -999.0;
  float humidityPct = -999.0;
  float t_a = dht.readTemperature();
  float h_a = dht.readHumidity();
  if (!isnan(t_a) && t_a > -40.0 && t_a < 80.0) ambTempC = t_a;
  if (!isnan(h_a) && h_a >= 0.0 && h_a <= 100.0) humidityPct = h_a;

  // Refresh OLED screen
  String wifiText = (WiFi.status() == WL_CONNECTED) ? "ONLINE" : "OFFLINE";
  updateOLED(voltageV, currentA, powerW, panelTempC, ambTempC, humidityPct, wifiText);

  // Telemetry Transmission Timer (Every 15 Seconds)
  if (currentMillis - lastSendMillis >= TELEMETRY_INTERVAL_MS || lastSendMillis == 0) {
    lastSendMillis = currentMillis;

    // Verify Wi-Fi connectivity
    if (WiFi.status() != WL_CONNECTED) {
      connectWiFi();
      if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WARN] Wi-Fi offline. Skipping transmission.");
        return;
      }
    }

    // Print reading summary to Serial
    Serial.println("--------------------------------------------------");
    Serial.printf("[TELEMETRY] V: %.2f V | I: %.3f A | P: %.2f W\n", voltageV, currentA, powerW);
    Serial.printf("[TEMPS]     Panel: %.1f C | Air: %.1f C | Humidity: %.0f %%\n",
                  panelTempC > -900 ? panelTempC : 0.0,
                  ambTempC > -900 ? ambTempC : 0.0,
                  humidityPct >= 0 ? humidityPct : 0.0);

    // Build JSON Payload
    String payload = "{";
    payload += "\"voltage\":" + String(voltageV, 3) + ",";
    payload += "\"current\":" + String(currentA, 3);
    if (panelTempC > -900) {
      payload += ",\"panel_temp\":" + String(panelTempC, 2);
    }
    if (ambTempC > -900) {
      payload += ",\"ambient_temp\":" + String(ambTempC, 2);
    }
    if (humidityPct >= 0) {
      payload += ",\"humidity\":" + String(humidityPct, 1);
    }
    payload += "}";

    // Transmit HTTP POST to Flask Backend
    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(5000);

    int httpCode = http.POST(payload);

    if (httpCode > 0) {
      if (httpCode == HTTP_CODE_CREATED || httpCode == HTTP_CODE_OK) {
        Serial.printf("[HTTP %d] Telemetry successfully stored in database.\n", httpCode);
      } else {
        Serial.printf("[HTTP %d] Server response: %s\n", httpCode, http.getString().c_str());
      }
    } else {
      Serial.printf("[HTTP ERROR] Failed to connect to %s (Error: %s)\n",
                    SERVER_URL, http.errorToString(httpCode).c_str());
    }

    http.end();
  }

  delay(200); // Small loop delay for smooth display refresh
}
