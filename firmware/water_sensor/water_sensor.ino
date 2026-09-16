// AquaSentry tank sensor firmware — NodeMCU/ESP8266 + HC-SR04 ultrasonic sensor.
// Publishes distance readings (sensor-to-water-surface, in cm) to MQTT every
// READ_INTERVAL ms. Consumed by main.py's water/distance handler.
//
// Wiring: HC-SR04 TRIG -> D5, ECHO -> D6 (via a voltage divider — ECHO is 5V,
// ESP8266 GPIO is 3.3V tolerant only).
//
// Libraries required: PubSubClient (knolleary/pubsubclient)
//
// Setup: copy secrets.h.example to secrets.h and fill in your WiFi/MQTT details.

#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <math.h>
#include "secrets.h"

#define TRIG_PIN D5
#define ECHO_PIN D6

#define SENSOR_MIN_CM   22
#define SENSOR_MAX_CM   450
#define NUM_SAMPLES     7
#define READ_INTERVAL   5000  // 5 seconds

// The tank fills/drains slowly (full 90->15cm swing takes ~15+ minutes, so
// well under ~2cm per READ_INTERVAL even at the fastest observed fill rate).
// A single-cycle jump bigger than this is noise, not real water movement —
// most likely long/marginal TRIG-ECHO wiring picking up interference. Require
// a jump to repeat a few times in a row before trusting it as a real change
// (e.g. after physically moving/re-mounting the sensor).
#define MAX_JUMP_CM         15.0
#define OUTLIER_PERSIST_COUNT 3

WiFiClient espClient;
PubSubClient client(espClient);

float lastGoodDistance = -1;
int outlierStreak = 0;

float getDistance() {
  float readings[NUM_SAMPLES];
  int validCount = 0;

  for (int i = 0; i < NUM_SAMPLES; i++) {
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(2);
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);

    long duration = pulseIn(ECHO_PIN, HIGH, 30000);
    if (duration > 0) {
      float dist = duration * 0.034 / 2.0;
      if (dist >= SENSOR_MIN_CM && dist <= SENSOR_MAX_CM) {
        readings[validCount] = dist;
        validCount++;
      }
    }
    delay(60);
  }

  if (validCount == 0) return -1;

  for (int i = 1; i < validCount; i++) {
    float key = readings[i];
    int j = i - 1;
    while (j >= 0 && readings[j] > key) {
      readings[j + 1] = readings[j];
      j--;
    }
    readings[j + 1] = key;
  }

  return readings[validCount / 2];
}

void connectWiFi() {
  Serial.print("[WiFi] Connecting to SSID: ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.print("\n[WiFi] Connected! IP: ");
  Serial.print(WiFi.localIP());
  Serial.print(" RSSI: ");
  Serial.println(WiFi.RSSI());
}

void connectMQTT() {
  client.setServer(mqtt_server, 1883);
  while (!client.connected()) {
    Serial.print("[MQTT] Connecting to ");
    Serial.print(mqtt_server);
    Serial.print(" ... ");
    if (client.connect("NodeMCU_WaterSensor")) {
      Serial.println("connected!");
    } else {
      Serial.print("failed, state=");
      Serial.print(client.state());
      Serial.println(" retrying in 1s");
      delay(1000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  connectWiFi();
  connectMQTT();
}

void loop() {
  // Keep MQTT connection alive
  if (!client.connected()) {
    connectMQTT();
  }
  client.loop();

  // Keep WiFi alive
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  // Read sensor first before any WiFi activity
  float dist = getDistance();
  Serial.println("Distance: " + String(dist) + "cm");

  if (dist == -1) {
    Serial.println("[Sensor] Invalid reading - skipping");
  } else if (lastGoodDistance != -1 && fabs(dist - lastGoodDistance) > MAX_JUMP_CM
             && ++outlierStreak < OUTLIER_PERSIST_COUNT) {
    Serial.print("[Sensor] Outlier rejected (jump from ");
    Serial.print(lastGoodDistance);
    Serial.print(" to ");
    Serial.print(dist);
    Serial.print("cm), streak=");
    Serial.println(outlierStreak);
  } else {
    outlierStreak = 0;
    lastGoodDistance = dist;
    char distStr[8];
    dtostrf(dist, 4, 2, distStr);
    bool ok = client.publish("water/distance", distStr);
    Serial.print("[MQTT] Publish ");
    Serial.print(distStr);
    Serial.println(ok ? " -> ok" : " -> FAILED");
  }

  delay(READ_INTERVAL);
}
