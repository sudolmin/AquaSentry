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
#include "secrets.h"

#define TRIG_PIN D5
#define ECHO_PIN D6

#define SENSOR_MIN_CM   22
#define SENSOR_MAX_CM   450
#define NUM_SAMPLES     7
#define READ_INTERVAL   5000  // 5 seconds

WiFiClient espClient;
PubSubClient client(espClient);

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
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected!");
}

void connectMQTT() {
  client.setServer(mqtt_server, 1883);
  while (!client.connected()) {
    Serial.print("Connecting to MQTT...");
    if (client.connect("NodeMCU_WaterSensor")) {
      Serial.println("connected!");
    } else {
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

  if (dist != -1) {
    char distStr[8];
    dtostrf(dist, 4, 2, distStr);
    client.publish("water/distance", distStr);
    Serial.print("Published: ");
    Serial.println(distStr);
  } else {
    Serial.println("Invalid reading - skipping");
  }

  delay(READ_INTERVAL);
}
