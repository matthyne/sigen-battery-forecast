# CLAUDE.md — Sigen Battery Forecast

## What this is

Python service that generates 7-day battery state-of-charge forecasts for Sigen solar + battery systems.

Runs daily via `battery-forecast.service` / `battery-forecast.timer` (systemd, user `matt`, 06:00 trigger).

**Repository location:** `~/projects/sigen-battery-forecast` (moved 2026-05-22 from `~/battery-forecast`)

## How it works

1. **Calibrates panel output** — 90 days of PV generation from InfluxDB + 30 days of Open-Meteo solar radiation data → computes effective panel factor
2. **Predicts house load** — day-of-week averages from 90 days of historical consumption, adjusted for temperature extremes
3. **Simulates battery SoC** — forward-projects 7 days applying net energy (PV − load); battery clamped 20%–100%; flags days where it would exhaust
4. **Writes predictions** — writes forecast to InfluxDB `battery_forecast` measurement (one point per day at midnight local)
5. **Logs summary** — prints table to stdout/systemd journal

## Key behavior

- **No hard-coded panel capacity** — infers from historical data
- **Temperature-aware load** — AC load jumps up on days >30°C or <8°C
- **Exhaust flagging** — marks days where battery would hit 20% minimum
- **Open-Meteo integration** — fetches 7-day weather forecast from open-meteo.com (free, no API key)

## Data sources

- **InfluxDB bucket:** `solar` (measurement: `sigen_plant`)
  - Reads: `pv_power_kw`, `total_load_power_kw`, `battery_soc_pct`
  - Writes: `battery_forecast` measurement
- **Open-Meteo:** 7-day forecast (temperature, solar radiation)

## Systemd

```bash
# View service
systemctl status battery-forecast.service
systemctl status battery-forecast.timer

# View logs
journalctl -u battery-forecast.service -f

# Manually trigger
sudo systemctl start battery-forecast.service

# Restart timer
sudo systemctl restart battery-forecast.timer
```

## Git workflow

```bash
cd ~/projects/sigen-battery-forecast
git pull
# edit files
git add <file>
git commit -m "description"
git push
```
