# battery-forecast

7-day battery state-of-charge forecast for a solar + battery system (Sigen inverter).

Runs daily, pulls historical data from InfluxDB, fetches a weather forecast from [Open-Meteo](https://open-meteo.com), simulates the next 7 days of battery charge/discharge, and writes the predictions back to InfluxDB for display in Grafana.

## How it works

1. **Calibrates panel output** — queries 90 days of actual PV generation from InfluxDB and 30 days of archive solar radiation from Open-Meteo. Computes the median `kWh_generated / kWh_m²_radiation` ratio as an effective panel factor. No hard-coded panel capacity required.

2. **Predicts house load** — computes day-of-week averages from 90 days of `total_load_power_kw` in InfluxDB. Adds an AC load adjustment on days where the forecast max temperature exceeds 30 °C or min temperature drops below 8 °C.

3. **Simulates battery SoC** — starting from the current SoC, steps through each forecast day applying net energy (PV − load). Battery is clamped between the minimum SoC (20%) and 100%. Days where the battery would hit minimum SoC are flagged as `will_exhaust`.

4. **Writes predictions to InfluxDB** — one data point per forecast day (midnight local time) in the `battery_forecast` measurement.

5. **Prints a summary table** to stdout / systemd journal.

## Requirements

- Python 3.10+
- InfluxDB v2 with a `solar` bucket containing `sigen_plant` measurements (`total_load_power_kw`, `pv_power_kw`, `battery_soc_pct`)
- Internet access for Open-Meteo API calls (free, no API key)

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `LATITUDE` | Location latitude (decimal degrees) |
| `LONGITUDE` | Location longitude (decimal degrees) |
| `INFLUXDB_URL` | InfluxDB v2 URL, e.g. `http://192.168.1.10:8086` |
| `INFLUXDB_TOKEN` | InfluxDB token with read access to your bucket and write access for `battery_forecast` |
| `INFLUXDB_ORG` | InfluxDB organisation name |
| `INFLUXDB_BUCKET` | InfluxDB bucket name (default: `solar`) |

The following constants at the top of `battery_forecast.py` can be tuned to match your system:

| Constant | Default | Description |
|---|---|---|
| `BATTERY_CAPACITY_KWH` | `48.5` | Usable battery capacity |
| `MIN_SOC_PCT` | `20` | Minimum SoC before grid supplements |
| `AC_HOT_THRESHOLD_C` | `30.0` | Max daily temp above which AC load is added |
| `AC_COLD_THRESHOLD_C` | `8.0` | Min daily temp below which AC load is added |
| `AC_LOAD_KWH` | `8.0` | Estimated AC energy consumption on an AC day |

## Running manually

```bash
python3 battery_forecast.py
```

Example output:

```
=== 7-Day Battery Forecast ===

Fetching historical data from InfluxDB ... done  (85 load days, 85 PV days)
Fetching archive radiation for PV calibration ... done  (calibrated panel factor: 15.2 kWp)
Fetching 7-day weather forecast ... done

Current SoC : 98.2%  (47.6 kWh / 48.5 kWh)
Panel factor: 15.2 kWp  |  Min SoC: 20%  (9.7 kWh)

Date         Day       PV    Load     Net   SoC%  Notes
──────────────────────────────────────────────────────────
2026-04-10   Fri    60.7k   54.5k   +6.1k 100.0%  AC hot (33°C)
2026-04-11   Sat    80.8k   38.4k  +42.4k 100.0%
2026-04-12   Sun    81.5k   22.2k  +59.3k 100.0%
...

✓  Battery should remain above minimum SoC for all 7 days.
```

## Systemd timer (daily at 06:00)

Copy the service and timer units, then enable:

```bash
sudo cp battery-forecast.service /etc/systemd/system/
sudo cp battery-forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now battery-forecast.timer
```

Check status and logs:

```bash
systemctl status battery-forecast.timer
journalctl -u battery-forecast.service -n 50
```

The `battery-forecast.service` unit file assumes the script lives at `/home/matt/amber-control/battery_forecast.py`. Adjust `ExecStart` and `WorkingDirectory` if you install it elsewhere.

## InfluxDB output

Data is written to the configured bucket under the measurement `battery_forecast`:

| Field | Type | Description |
|---|---|---|
| `predicted_load_kwh` | float | Forecast house consumption for the day |
| `predicted_pv_kwh` | float | Forecast solar generation for the day |
| `predicted_net_kwh` | float | Net energy (PV − load); negative = battery/grid draw |
| `predicted_soc_pct` | float | Predicted battery SoC at end of day |
| `will_exhaust` | float | `1.0` if battery is predicted to hit minimum SoC, else `0.0` |
| `max_temp_c` | float | Forecast maximum temperature for the day |

Timestamps are set to midnight local time (Australia/Sydney) for each forecast day, so data points land in the future. Grafana panels querying this measurement should use `range(start: now(), stop: 8d)`.

## Grafana integration

The companion [monitoring](https://github.com/matthyne/monitoring) repo includes a Grafana dashboard (`sigen-solar.json`) with a **Forecast** section containing:

- **Min Predicted SoC** — stat panel, green/yellow/red thresholds
- **Days Battery Will Be Low** — stat panel counting days with `will_exhaust = 1`
- **Battery SoC — Actual & Forecast** — timeseries overlaying recent actual SoC with the 7-day predicted SoC (dashed orange)
- **Predicted Load vs PV** — timeseries for the 7-day load and generation forecast
- **Alert** — fires when minimum predicted SoC over the next 7 days drops below 25%

The dashboard default time range is set to `now-2d` → `now+8d` so future forecast data points are visible.

## Data sources

- **Historical load & PV**: InfluxDB `sigen_plant` measurement (`total_load_power_kw`, `pv_power_kw`)
- **Archive radiation**: [Open-Meteo Historical API](https://open-meteo.com/en/docs/historical-weather-api) — free, no key required
- **Forecast weather**: [Open-Meteo Forecast API](https://open-meteo.com/en/docs) — free, no key required
