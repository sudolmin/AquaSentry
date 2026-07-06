# Home Assistant setup

These files implement the pump-control side of AquaSentry: turning the pump on/off based on tank level, with layered safety protections. The dashboard app (`main.py`) only *observes* MQTT topics — all pump control logic lives here, in Home Assistant.

## Setup

1. Add the contents of `configuration.yaml.snippet` to your HA `configuration.yaml` (or merge the `mqtt:` block if you already have one).
2. Copy `automations.yaml` into your HA config, or merge its entries into your existing `automations.yaml`.
3. Replace the placeholder entity IDs with your own:
   - `switch.pump` → your smart plug's switch entity
   - `sensor.pump_power`, `sensor.pump_voltage`, `sensor.pump_current`, `sensor.pump_energy_today`, `sensor.pump_energy_month`, `binary_sensor.pump_overloaded` → your smart plug's power-monitoring entities (if it has them — dry-run protection and telemetry publishing depend on these; skip those two automations if your plug doesn't report power)
4. Tune the thresholds for your own tank and pump:
   - `below: 25` / `above: 70` (cm) — your tank's full/low distance thresholds
   - `below: 900` (W) — your pump's dry-run wattage threshold. **Don't reuse this value** — it depends entirely on your pump's normal loaded wattage. Measure your pump's steady-state power draw with water flowing, then set the threshold at roughly 65-75% of that (see the main project history/discussion for how this was derived).
   - `minutes: 18` safety timeout — should exceed your normal fill time with margin, but stay short enough to limit damage if every other automation fails.
5. Restart Home Assistant, then run `automation reload` or a full restart to pick up changes.

## Why the design looks the way it does

- **Tank Full is edge-triggered with a fixed delay, not a "stay below threshold" debounce.** Ultrasonic sensors get noisy near the water surface (splashing, turbulence) — a "must stay continuously below threshold for N seconds" check can fail indefinitely if the sensor spikes back above threshold every 30-60 seconds, even while the tank is genuinely full. A fixed delay from first detection sidesteps that.
- **Turn ON requires 60s sustained above threshold.** A single spurious high reading (sensor noise) could otherwise start the pump on a tank that isn't actually low.
- **Dry-run protection uses the smart plug's own power sensor**, not tank level — it detects the pump spinning without hydraulic load (a genuine sign of a dry well), independent of anything wrong with the tank sensor.
- **`initial_state: true` on every automation that can shut the pump off.** Without it, an automation disabled via the HA UI (deliberately or accidentally) silently stays disabled across restarts — which is exactly how a real safety gap showed up during development.
