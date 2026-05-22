#!/usr/bin/env python3
"""
battery_forecast.py — 7-day battery SoC forecast for the Sigen system

Method:
  - Calibrates PV output factor from 30 days of actual InfluxDB PV data
    vs Open-Meteo archive radiation — no hard-coded panel capacity needed
  - Forecasts house load from 90-day day-of-week consumption averages
  - Adds AC load on days where max temp > 30°C or min temp < 8°C
  - Simulates battery SoC day-by-day (min SoC = 20%, capacity = 48.5 kWh)
  - Writes predictions to InfluxDB measurement 'battery_forecast'
  - Prints a 7-day summary table

Run daily via systemd timer (see battery-forecast.timer).
"""

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── Config ─────────────────────────────────────────────────────────────────────
BATTERY_CAPACITY_KWH = 48.5
MIN_SOC_PCT          = 20       # battery won't discharge below this (self-use mode)
AC_HOT_THRESHOLD_C   = 30.0    # max daily temp above this → add AC load
AC_COLD_THRESHOLD_C  = 8.0     # min daily temp below this → add AC load
AC_LOAD_KWH          = 8.0     # estimated AC energy on a hot/cold day

LAT = float(os.environ["LATITUDE"])
LON = float(os.environ["LONGITUDE"])
TZ  = ZoneInfo("Australia/Sydney")

INFLUX_URL    = os.environ.get("INFLUXDB_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.environ["INFLUXDB_TOKEN"]
INFLUX_ORG    = os.environ.get("INFLUXDB_ORG",    "hyne")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "solar")

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE  = "https://archive-api.open-meteo.com/v1/archive"

# ── InfluxDB ───────────────────────────────────────────────────────────────────

def flux_query(client, query: str) -> list[dict]:
    result = client.query_api().query(org=INFLUX_ORG, query=query)
    return [r.values for table in result for r in table.records]


def get_historical_daily_totals(client) -> tuple[dict, dict]:
    """
    Returns (load_by_date, pv_by_date) as {date_str: kwh} for the last 90 days.
    Uses integral(unit: 1h) per day, aligned to Sydney timezone.
    """
    rows = flux_query(client, '''
import "timezone"
option location = timezone.location(name: "Australia/Sydney")

from(bucket: "solar")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "sigen_plant" and
      (r._field == "total_load_power_kw" or r._field == "pv_power_kw"))
  |> window(every: 1d, location: timezone.location(name: "Australia/Sydney"))
  |> integral(unit: 1h)
  |> duplicate(column: "_stop", as: "_time")
  |> window(every: inf)
  |> keep(columns: ["_time", "_field", "_value"])
''')

    load_by_date: dict[str, float] = {}
    pv_by_date:   dict[str, float] = {}

    for row in rows:
        t = row["_time"]
        if isinstance(t, datetime):
            t = t.astimezone(TZ)
        # _stop is midnight of the NEXT day (window end), so the consumption
        # date is t.date() - 1 day
        d = (t.date() - timedelta(days=1)).isoformat()
        val = float(row.get("_value") or 0.0)
        if row.get("_field") == "total_load_power_kw":
            load_by_date[d] = val
        else:
            pv_by_date[d] = val

    return load_by_date, pv_by_date


def get_current_soc(client) -> float:
    rows = flux_query(client, '''
from(bucket: "solar")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "sigen_plant" and r._field == "battery_soc_pct")
  |> last()
''')
    return float(rows[0]["_value"]) if rows else 50.0


def write_forecast(client, forecasts: list[dict]) -> None:
    write_api = client.write_api(write_options=SYNCHRONOUS)
    points = []
    for f in forecasts:
        # Timestamp: midnight Sydney time for each forecast date
        ts = datetime(
            f["date"].year, f["date"].month, f["date"].day,
            0, 0, 0, tzinfo=TZ
        ).astimezone(timezone.utc)
        p = (
            Point("battery_forecast")
            .time(ts, WritePrecision.S)
            .field("predicted_load_kwh", round(f["load"],           2))
            .field("predicted_pv_kwh",   round(f["pv"],             2))
            .field("predicted_net_kwh",  round(f["pv"] - f["load"], 2))
            .field("predicted_soc_pct",  round(f["soc_end"],        1))
            .field("will_exhaust",       1.0 if f["exhaust"] else 0.0)
            .field("max_temp_c",         round(f["max_temp"],       1))
        )
        points.append(p)
    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)


# ── Weather ────────────────────────────────────────────────────────────────────

def get_archive_radiation(start: date, end: date) -> dict[str, float]:
    """Daily total radiation kWh/m² from Open-Meteo archive."""
    resp = requests.get(OPEN_METEO_ARCHIVE, params={
        "latitude":   LAT, "longitude": LON,
        "start_date": str(start), "end_date": str(end),
        "hourly":     "shortwave_radiation",
        "timezone":   "Australia/Sydney",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()["hourly"]
    daily: dict[str, float] = {}
    for t, r in zip(data["time"], data["shortwave_radiation"]):
        daily[t[:10]] = daily.get(t[:10], 0.0) + (r or 0.0) / 1000.0
    return daily


def get_forecast_weather() -> dict[str, dict]:
    """7-day forecast: radiation (kWh/m²) and temperature per date string."""
    resp = requests.get(OPEN_METEO_FORECAST, params={
        "latitude":      LAT, "longitude": LON,
        "hourly":        "shortwave_radiation",
        "daily":         "temperature_2m_max,temperature_2m_min",
        "forecast_days": 8,
        "timezone":      "Australia/Sydney",
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    daily_rad: dict[str, float] = {}
    for t, r in zip(data["hourly"]["time"], data["hourly"]["shortwave_radiation"]):
        d = t[:10]
        daily_rad[d] = daily_rad.get(d, 0.0) + (r or 0.0) / 1000.0

    today_str = date.today().isoformat()
    result = {}
    for d, tmax, tmin in zip(
        data["daily"]["time"],
        data["daily"]["temperature_2m_max"],
        data["daily"]["temperature_2m_min"],
    ):
        if d <= today_str:
            continue
        result[d] = {
            "radiation_kwh_m2": daily_rad.get(d, 0.0),
            "max_temp":         float(tmax or 20.0),
            "min_temp":         float(tmin or 10.0),
        }
    return result


# ── Calibration ────────────────────────────────────────────────────────────────

def calibrate_panel_factor(pv_by_date: dict, archive_rad: dict) -> float:
    """
    Derives effective kWp from actual PV output vs archive radiation.
    Uses days with >2 kWh PV and >1 kWh/m² radiation; returns the median ratio.
    This avoids needing a hard-coded panel capacity.
    """
    ratios = []
    for d, pv in pv_by_date.items():
        rad = archive_rad.get(d, 0.0)
        if pv > 2.0 and rad > 1.0:
            ratios.append(pv / rad)
    if not ratios:
        return 5.0  # conservative fallback
    ratios.sort()
    return ratios[len(ratios) // 2]  # median


# ── Day-of-week averages ───────────────────────────────────────────────────────

def dow_averages(by_date: dict, min_val: float = 0.5) -> dict[int, float]:
    """Groups {date_str: kwh} by weekday (0=Mon), returns {dow: avg}."""
    buckets: dict[int, list[float]] = {i: [] for i in range(7)}
    for d, val in by_date.items():
        if val > min_val:
            buckets[date.fromisoformat(d).weekday()].append(val)
    return {dow: (sum(v) / len(v) if v else 15.0) for dow, v in buckets.items()}


# ── Simulation ─────────────────────────────────────────────────────────────────

def simulate(
    current_soc_pct: float,
    load_dow_avg: dict,
    panel_factor: float,
    weather: dict,
) -> list[dict]:
    battery_kwh = current_soc_pct / 100.0 * BATTERY_CAPACITY_KWH
    min_kwh     = MIN_SOC_PCT / 100.0 * BATTERY_CAPACITY_KWH
    today       = date.today()
    results     = []

    for offset in range(1, 8):
        fdate    = today + timedelta(days=offset)
        dstr     = fdate.isoformat()
        dow      = fdate.weekday()
        wx       = weather.get(dstr, {})

        pv       = panel_factor * wx.get("radiation_kwh_m2", 0.0)
        max_temp = wx.get("max_temp", 20.0)
        min_temp = wx.get("min_temp", 10.0)

        base_load = load_dow_avg.get(dow, 15.0)
        ac_load   = 0.0
        ac_note   = ""
        if max_temp > AC_HOT_THRESHOLD_C:
            ac_load = AC_LOAD_KWH
            ac_note = f"hot ({max_temp:.0f}°C)"
        elif min_temp < AC_COLD_THRESHOLD_C:
            ac_load = AC_LOAD_KWH
            ac_note = f"cold ({min_temp:.0f}°C)"

        load      = base_load + ac_load
        net       = pv - load
        soc_start = battery_kwh / BATTERY_CAPACITY_KWH * 100.0

        if net >= 0:
            battery_kwh = min(battery_kwh + net, BATTERY_CAPACITY_KWH)
            exhaust = False
        else:
            available = battery_kwh - min_kwh
            if abs(net) > available:
                battery_kwh = min_kwh
                exhaust = True
            else:
                battery_kwh += net  # net is negative
                exhaust = False

        results.append({
            "date":      fdate,
            "dow":       dow,
            "pv":        pv,
            "load":      load,
            "base_load": base_load,
            "ac_load":   ac_load,
            "ac_note":   ac_note,
            "max_temp":  max_temp,
            "min_temp":  min_temp,
            "soc_start": soc_start,
            "soc_end":   battery_kwh / BATTERY_CAPACITY_KWH * 100.0,
            "exhaust":   exhaust,
        })

    return results


# ── Main ───────────────────────────────────────────────────────────────────────

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def main() -> None:
    print("=== 7-Day Battery Forecast ===\n")

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=60000) as client:
        print("Fetching historical data from InfluxDB ...", end=" ", flush=True)
        load_by_date, pv_by_date = get_historical_daily_totals(client)
        current_soc = get_current_soc(client)
        print(f"done  ({len(load_by_date)} load days, {len(pv_by_date)} PV days)")

        print("Fetching archive radiation for PV calibration ...", end=" ", flush=True)
        cal_end   = date.today() - timedelta(days=1)
        cal_start = cal_end - timedelta(days=29)
        archive_rad  = get_archive_radiation(cal_start, cal_end)
        panel_factor = calibrate_panel_factor(pv_by_date, archive_rad)
        print(f"done  (calibrated panel factor: {panel_factor:.1f} kWp)")

        print("Fetching 7-day weather forecast ...", end=" ", flush=True)
        weather = get_forecast_weather()
        print("done\n")

        load_dow = dow_averages(load_by_date)
        pv_dow   = dow_averages(pv_by_date)

        print(f"Current SoC : {current_soc:.1f}%  "
              f"({current_soc / 100 * BATTERY_CAPACITY_KWH:.1f} kWh / {BATTERY_CAPACITY_KWH} kWh)")
        print(f"Panel factor: {panel_factor:.1f} kWp  |  "
              f"Min SoC: {MIN_SOC_PCT}%  "
              f"({MIN_SOC_PCT / 100 * BATTERY_CAPACITY_KWH:.1f} kWh)\n")

        forecasts = simulate(current_soc, load_dow, panel_factor, weather)

        header = (f"{'Date':<12} {'Day'}  {'PV':>7} {'Load':>7} {'Net':>7} "
                  f"{'SoC%':>6}  Notes")
        print(header)
        print("─" * len(header))

        any_exhaust = False
        for f in forecasts:
            notes = []
            if f["exhaust"]:
                notes.append("⚠ BATTERY LOW")
                any_exhaust = True
            if f["ac_note"]:
                notes.append(f"AC {f['ac_note']}")
            net = f["pv"] - f["load"]
            print(
                f"{f['date'].isoformat():<12} {DAYS[f['dow']]}  "
                f"{f['pv']:>6.1f}k {f['load']:>6.1f}k {net:>+6.1f}k "
                f"{f['soc_end']:>5.1f}%  {'  '.join(notes)}"
            )

        print()
        if any_exhaust:
            print("⚠  Battery predicted to hit minimum SoC — grid will supplement on those days.")
        else:
            print("✓  Battery should remain above minimum SoC for all 7 days.")
        print()

        print("Writing forecast to InfluxDB ...", end=" ", flush=True)
        write_forecast(client, forecasts)
        print("done")


if __name__ == "__main__":
    main()
