# Firmware

`water_sensor.ino` — the NodeMCU/ESP8266 sketch that reads the HC-SR04 ultrasonic sensor and publishes to `water/distance` over MQTT.

## Setup

1. Copy `secrets.h.example` to `secrets.h` and fill in your WiFi/MQTT details. `secrets.h` is gitignored.
2. Install the **PubSubClient** library (Arduino Library Manager: `knolleary/pubsubclient`).
3. Wire the HC-SR04: `TRIG` → D5, `ECHO` → D6 (through a voltage divider — ECHO outputs 5V, ESP8266 GPIOs are 3.3V-tolerant only).
4. Flash and it'll publish a median-of-7-samples distance reading every 5 seconds.

## Known limitation

Ultrasonic sensors have a real near-field blind spot — readings below ~22cm are unreliable, and even above that, readings can get noisy near the water's surface (splashing/turbulence while filling). `SENSOR_MIN_CM` filters out physically-impossible readings, but doesn't fully eliminate noise in the 25-40cm range. The Home Assistant automations (see [`../home-assistant/`](../home-assistant/)) are designed around this — see that folder's README for why the pump-control logic works the way it does.
