/*
 * Fire Evacuation Router -- Wokwi ESP32 firmware port.
 *
 * Demonstrates the same sensor-fusion formula used in the full MQTT-based
 * system (node/fire_node.py + node/routing_common.py), ported to run on
 * real/simulated hardware. This sketch only reproduces the fusion +
 * local LED/color logic for one zone; it does not implement MQTT,
 * multi-node routing, or Dijkstra pathfinding.
 *
 * Wiring:
 *   DHT22 data        -> GPIO 4   (temperature)
 *   Potentiometer SIG  -> GPIO 34 (analog, stand-in for smoke ppm sensor)
 *   Pushbutton         -> GPIO 5, INPUT_PULLUP (stand-in for flame sensor;
 *                          pressed/LOW means flame detected)
 *   RGB LED            -> R: GPIO 25, G: GPIO 26, B: GPIO 27 (common cathode)
 */

#include <DHT.h>

#define DHT_PIN 4
#define DHT_TYPE DHT22

#define SMOKE_PIN 34
#define FLAME_BUTTON_PIN 5

#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27

// Sensor fusion constants -- same values as ALPHA/BETA/FLAME_PENALTY in
// node/routing_common.py. Ambient baseline here is 25 C per this sketch's
// spec; the Python side currently uses AMBIENT_C = 22.0.
const float ALPHA = 0.05;
const float BETA = 0.002;
const float AMBIENT_C = 25.0;
const float FLAME_PENALTY = 1000.0;

// Color thresholds on cost -- match YELLOW_COST_THRESHOLD / RED_COST_THRESHOLD
// in node/routing_common.py.
const float YELLOW_COST_THRESHOLD = 5.0;
const float RED_COST_THRESHOLD = 50.0;

const int ANALOG_MAX = 4095;
const float SMOKE_PPM_MAX = 500.0;

const unsigned long LOOP_INTERVAL_MS = 1000;

DHT dht(DHT_PIN, DHT_TYPE);

void setLedColor(bool red, bool green, bool blue) {
  digitalWrite(LED_R_PIN, red ? LOW : HIGH);
  digitalWrite(LED_G_PIN, green ? LOW : HIGH);
  digitalWrite(LED_B_PIN, blue ? LOW : HIGH);
}

float zoneCost(float tempC, float smokePpm, bool flame) {
  float cost = 1.0;
  cost += ALPHA * max(0.0f, tempC - AMBIENT_C);
  cost += BETA * pow(smokePpm, 1.5);
  if (flame) {
    cost += FLAME_PENALTY;
  }
  return cost;
}

void setup() {
  Serial.begin(115200);
  dht.begin();

  pinMode(FLAME_BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_R_PIN, OUTPUT);
  pinMode(LED_G_PIN, OUTPUT);
  pinMode(LED_B_PIN, OUTPUT);

  setLedColor(false, false, false);
}

void loop() {
  float tempC = dht.readTemperature();
  if (isnan(tempC)) {
    Serial.println("DHT22 read failed, skipping this cycle");
    delay(LOOP_INTERVAL_MS);
    return;
  }

  int rawSmoke = analogRead(SMOKE_PIN);
  float smokePpm = (rawSmoke / (float)ANALOG_MAX) * SMOKE_PPM_MAX;

  bool flame = (digitalRead(FLAME_BUTTON_PIN) == LOW);

  float cost = zoneCost(tempC, smokePpm, flame);

  if (flame || cost >= RED_COST_THRESHOLD) {
    setLedColor(true, false, false);
    Serial.println("SHELTER IN PLACE");
  } else if (cost >= YELLOW_COST_THRESHOLD) {
    setLedColor(true, true, false);
  } else {
    setLedColor(false, true, false);
  }

  Serial.print("temp=");
  Serial.print(tempC);
  Serial.print(" smoke_ppm=");
  Serial.print(smokePpm);
  Serial.print(" flame=");
  Serial.print(flame ? "true" : "false");
  Serial.print(" cost=");
  Serial.println(cost);

  delay(LOOP_INTERVAL_MS);
}
