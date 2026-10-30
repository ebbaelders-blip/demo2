"""Informasjonsskjerm for vær og bussavganger ved Halden videregående skole."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
LATITUDE = float(os.getenv("HALDEN_LAT", "59.12521"))
LONGITUDE = float(os.getenv("HALDEN_LON", "11.39479"))
BUS_RADIUS_METERS = int(os.getenv("BUS_RADIUS_METERS", "1400"))
WEATHER_USER_AGENT = os.getenv("WEATHER_USER_AGENT", "HaldenInformasjonsskjerm/1.0")
ENTUR_CLIENT_NAME = os.getenv("ENTUR_CLIENT_NAME", "halden-vgs-informasjonsskjerm")
CACHE_SECONDS = 60
CITY_LOCATIONS = {
    "halden": ("Halden", 59.12521, 11.39479),
    "goteborg": ("Gøteborg", 57.7089, 11.9746),
    "kobenhavn": ("København", 55.6761, 12.5683),
    "oslo": ("Oslo", 59.9139, 10.7522),
    "stockholm": ("Stockholm", 59.3293, 18.0686),
}

ENTUR_QUERY = """
{
  quaysByRadius(
    latitude: %s
    longitude: %s
    radius: %s
    first: 20
  ) {
    edges {
      node {
        distance
        quay {
          name
          estimatedCalls(numberOfDepartures: 5) {
            expectedDepartureTime
            aimedDepartureTime
            destinationDisplay { frontText }
            serviceJourney {
              journeyPattern {
                line { publicCode }
              }
            }
          }
        }
      }
    }
  }
}
""" % (LATITUDE, LONGITUDE, BUS_RADIUS_METERS)


def fetch_json(url: str, *, headers: dict[str, str], body: bytes | None = None) -> object:
    request = Request(url, data=body, headers=headers)
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def weather_data(payload: dict[str, object]) -> dict[str, object]:
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Yr svarte uten værdata.")

    timeseries = properties.get("timeseries")
    if not isinstance(timeseries, list) or not timeseries:
        raise ValueError("Yr har ingen tilgjengelige prognoser.")

    forecasts: list[dict[str, object]] = []
    for item in timeseries[:8]:
        if not isinstance(item, dict):
            continue
        details = item.get("data", {}).get("instant", {}).get("details", {})
        next_hours = item.get("data", {}).get("next_1_hours", {})
        summary = next_hours.get("summary", {})
        forecasts.append(
            {
                "time": item.get("time"),
                "temperature": details.get("air_temperature"),
                "wind": details.get("wind_speed"),
                "precipitation": next_hours.get("details", {}).get("precipitation_amount"),
                "symbol": summary.get("symbol_code", "cloudy"),
            }
        )

    current = forecasts[0] if forecasts else {}
    return {
        "current": current,
        "forecasts": forecasts[1:7],
        "source": "Yr / MET Norway",
    }


def load_city_weather(city_id: str) -> dict[str, object]:
    city = CITY_LOCATIONS.get(city_id)
    if city is None:
        raise ValueError(f"Ukjent by: {city_id}")

    _, latitude, longitude = city
    url = (
        "https://api.met.no/weatherapi/locationforecast/2.0/compact"
        f"?lat={latitude}&lon={longitude}"
    )
    payload = fetch_json(url, headers={"User-Agent": WEATHER_USER_AGENT})
    if not isinstance(payload, dict):
        raise ValueError("Yr svarte med et ugyldig dataformat.")
    return weather_data(payload)


def departure_data(payload: dict[str, object]) -> list[dict[str, object]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        errors = payload.get("errors")
        message = errors[0].get("message") if isinstance(errors, list) and errors else None
        raise ValueError(f"Entur svarte med en feil: {message or 'ukjent feil'}")

    quays = data.get("quaysByRadius", {}).get("edges", [])
    now = datetime.now(timezone.utc)
    departures: list[dict[str, object]] = []

    for edge in quays:
        node = edge.get("node", {})
        quay = node.get("quay", {})
        stop_name = quay.get("name") or "Holdeplass"
        distance = node.get("distance")
        for call in quay.get("estimatedCalls", []):
            expected = call.get("expectedDepartureTime")
            if not expected:
                continue
            departure_time = datetime.fromisoformat(expected)
            if departure_time.astimezone(timezone.utc) < now - timedelta(minutes=2):
                continue

            journey = call.get("serviceJourney") or {}
            pattern = journey.get("journeyPattern") or {}
            line = pattern.get("line") or {}
            destination = call.get("destinationDisplay") or {}
            departures.append(
                {
                    "time": expected,
                    "scheduledTime": call.get("aimedDepartureTime"),
                    "line": line.get("publicCode") or "Buss",
                    "destination": destination.get("frontText") or "Ukjent destinasjon",
                    "stop": stop_name,
                    "distance": round(distance) if isinstance(distance, (int, float)) else None,
                }
            )

    departures.sort(key=lambda item: item["time"])
    return departures[:12]


def load_weather() -> dict[str, object]:
    url = (
        "https://api.met.no/weatherapi/locationforecast/2.0/compact"
        f"?lat={LATITUDE}&lon={LONGITUDE}"
    )
    payload = fetch_json(url, headers={"User-Agent": WEATHER_USER_AGENT})
    if not isinstance(payload, dict):
        raise ValueError("Yr svarte med et ugyldig dataformat.")
    return weather_data(payload)


def load_departures() -> list[dict[str, object]]:
    payload = fetch_json(
        "https://api.entur.io/journey-planner/v3/graphql",
        headers={
            "ET-Client-Name": ENTUR_CLIENT_NAME,
            "Content-Type": "application/json; charset=utf-8",
        },
        body=json.dumps({"query": ENTUR_QUERY}).encode("utf-8"),
    )
    if not isinstance(payload, dict):
        raise ValueError("Entur svarte med et ugyldig dataformat.")
    return departure_data(payload)


_cache_lock = threading.Lock()
_cached_dashboard: dict[str, object] | None = None
_cache_expires_at = 0.0


def dashboard_data() -> dict[str, object]:
    global _cached_dashboard, _cache_expires_at
    with _cache_lock:
        if _cached_dashboard is not None and time.monotonic() < _cache_expires_at:
            return _cached_dashboard

        errors: dict[str, str] = {}
        weather: dict[str, object] | None = None
        departures: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = {
                "weather": executor.submit(load_weather),
                "departures": executor.submit(load_departures),
            }
            for name, task in tasks.items():
                try:
                    result = task.result()
                    if name == "weather":
                        weather = result
                    else:
                        departures = result
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                    errors[name] = str(error)

        _cached_dashboard = {
            "weather": weather,
            "departures": departures,
            "errors": errors,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        _cache_expires_at = time.monotonic() + CACHE_SECONDS
        return _cached_dashboard


_city_cache_lock = threading.Lock()
_cached_cities: dict[str, object] | None = None
_city_cache_expires_at = 0.0


def cities_weather_data() -> dict[str, object]:
    global _cached_cities, _city_cache_expires_at
    with _city_cache_lock:
        if _cached_cities is not None and time.monotonic() < _city_cache_expires_at:
            return _cached_cities

        cities: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=len(CITY_LOCATIONS)) as executor:
            tasks = {
                city_id: executor.submit(load_city_weather, city_id)
                for city_id in CITY_LOCATIONS
            }
            for city_id, (name, _, _) in CITY_LOCATIONS.items():
                try:
                    weather = tasks[city_id].result()
                    cities.append({"id": city_id, "name": name, **weather})
                except (
                    HTTPError,
                    URLError,
                    TimeoutError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    cities.append({"id": city_id, "name": name, "error": str(error)})

        _cached_cities = {
            "cities": cities,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        _city_cache_expires_at = time.monotonic() + CACHE_SECONDS
        return _cached_cities


HTML_PAGE = r"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101b2d">
  <title>Halden videregående skole – informasjonsskjerm</title>
  <style>
    :root {
      color-scheme: light;
      font-family: "Segoe UI", Arial, sans-serif;
      background: #101b2d;
      color: #f5f7fb;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
    }
    * { box-sizing: border-box; }
    body {
      min-width: 320px;
      min-height: 100vh;
      margin: 0;
      background:
        radial-gradient(ellipse at 12% 0%, #24375a 0, transparent 38%),
        radial-gradient(ellipse at 100% 100%, #173d48 0, transparent 34%),
        #101b2d;
    }
    .page { width: min(1540px, 100%); margin: 0 auto; padding: 38px 48px 24px; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 24px; margin-bottom: 32px; }
    .brand { display: flex; align-items: center; gap: 16px; }
    .site-nav { display: flex; align-items: center; gap: 9px; }
    .site-nav a { padding: 10px 14px; border: 1px solid #ffffff20; border-radius: 12px; color: #d7e3f3; font-size: 13px; text-decoration: none; }
    .site-nav a:hover { background: #ffffff10; }
    .brand-mark {
      display: grid; width: 52px; height: 52px; place-items: center;
      border: 1px solid #ffffff24; border-radius: 17px;
      background: linear-gradient(145deg, #7ab9ff, #526ee5);
      box-shadow: 0 8px 26px #526ee555; font-size: 25px;
    }
    .eyebrow { margin: 0 0 5px; color: #a7b8d2; font-size: 12px; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; }
    h1 { margin: 0; font-size: clamp(20px, 2.4vw, 29px); letter-spacing: -.03em; }
    .clock { text-align: right; }
    #clock { font-size: clamp(28px, 4vw, 43px); font-weight: 650; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
    #date { margin-top: 2px; color: #a7b8d2; font-size: 14px; text-transform: capitalize; }
    main { display: grid; grid-template-columns: minmax(0, .92fr) minmax(0, 1.08fr); gap: 22px; align-items: stretch; }
    .card { overflow: hidden; border: 1px solid #ffffff17; border-radius: 25px; background: #19263acc; box-shadow: 0 24px 70px #080f1c38; backdrop-filter: blur(12px); }
    .weather-card { min-height: 560px; padding: 29px; }
    .section-heading { display: flex; justify-content: space-between; align-items: center; gap: 14px; }
    h2 { margin: 0; font-size: 19px; letter-spacing: -.02em; }
    .live { display: inline-flex; align-items: center; gap: 7px; color: #b6c8df; font-size: 12px; }
    .live::before { width: 7px; height: 7px; border-radius: 50%; background: #59dbad; box-shadow: 0 0 12px #59dbad; content: ""; }
    .weather-now { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 47px 0 38px; }
    .temperature { font-size: clamp(72px, 11vw, 116px); line-height: .95; font-weight: 550; letter-spacing: -.085em; font-variant-numeric: tabular-nums; }
    .condition { margin: 12px 0 0 5px; color: #b9c9df; font-size: 17px; }
    .weather-icon { padding-right: 10px; font-size: clamp(62px, 9vw, 93px); filter: drop-shadow(0 8px 18px #f4c66b35); }
    .weather-details { display: flex; flex-wrap: wrap; gap: 10px; }
    .detail { flex: 1 1 130px; padding: 14px 15px; border: 1px solid #ffffff12; border-radius: 15px; background: #ffffff08; }
    .detail-label { color: #9fafc5; font-size: 12px; }
    .detail-value { margin-top: 6px; font-size: 18px; font-weight: 650; }
    .forecast { margin-top: 34px; }
    .forecast-title { margin: 0 0 14px; color: #a9bad0; font-size: 12px; font-weight: 700; letter-spacing: .13em; text-transform: uppercase; }
    .forecast-list { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; }
    .forecast-item { padding: 12px 6px; border-radius: 14px; background: #ffffff07; text-align: center; }
    .forecast-time { color: #a9bad0; font-size: 12px; }
    .forecast-icon { margin: 9px 0; font-size: 21px; }
    .forecast-temp { font-size: 15px; font-weight: 700; }
    .bus-card { min-height: 560px; padding: 29px 0 13px; }
    .bus-heading { padding: 0 29px 21px; }
    .bus-subtitle { margin: 6px 0 0; color: #a9bad0; font-size: 13px; }
    .bus-badge { padding: 7px 11px; border-radius: 999px; background: #6b79f12b; color: #bdc5ff; font-size: 12px; font-weight: 700; }
    .table-head, .departure { display: grid; grid-template-columns: 78px 68px minmax(110px, 1fr) 120px; align-items: center; gap: 12px; }
    .table-head { padding: 10px 29px; border-top: 1px solid #ffffff12; border-bottom: 1px solid #ffffff12; color: #91a2ba; font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    #departures { min-height: 315px; }
    .departure { min-height: 61px; padding: 9px 29px; border-bottom: 1px solid #ffffff0c; }
    .departure:last-child { border-bottom: 0; }
    .departure-time { font-size: 19px; font-weight: 700; font-variant-numeric: tabular-nums; }
    .departure-min { display: block; margin-top: 2px; color: #96a9c2; font-size: 11px; font-weight: 500; }
    .line { display: inline-grid; min-width: 43px; min-height: 31px; padding: 0 7px; place-items: center; border-radius: 10px; background: #6c7af2; color: #fff; font-size: 14px; font-weight: 800; }
    .destination { overflow: hidden; font-size: 14px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .stop { overflow: hidden; margin-top: 3px; color: #91a2ba; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .distance { color: #aab9cd; font-size: 12px; text-align: right; white-space: nowrap; }
    .message { display: grid; min-height: 290px; padding: 30px; place-content: center; color: #aab9cd; line-height: 1.6; text-align: center; }
    .message strong { display: block; margin-bottom: 6px; color: #eef3fb; font-size: 16px; }
    footer { display: flex; justify-content: space-between; gap: 14px; margin-top: 19px; color: #8293aa; font-size: 11px; }
    footer a { color: #a9bdd7; text-decoration: none; }
    #status { color: #a9bdd7; }
    @media (max-width: 900px) {
      .page { padding: 26px 24px 20px; }
      main { grid-template-columns: 1fr; }
      .weather-card, .bus-card { min-height: auto; }
      .weather-now { padding: 34px 0 28px; }
      #departures { min-height: 0; }
    }
    @media (max-width: 540px) {
      .page { padding: 20px 14px; }
      header { margin-bottom: 21px; }
      .brand { gap: 10px; }
      .brand-mark { width: 43px; height: 43px; border-radius: 13px; font-size: 21px; }
      .eyebrow { font-size: 10px; }
      #date { font-size: 11px; }
      .card { border-radius: 19px; }
      .weather-card { padding: 21px; }
      .weather-now { padding: 30px 0 25px; }
      .forecast-list { grid-template-columns: repeat(3, 1fr); }
      .table-head, .departure { grid-template-columns: 59px 48px minmax(75px, 1fr) 65px; gap: 8px; }
      .table-head { padding: 10px 13px; font-size: 9px; }
      .departure { padding: 9px 13px; }
      .bus-heading { padding: 0 17px 18px; }
      .destination { font-size: 12px; }
      .distance { font-size: 10px; }
      footer { flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <div class="page">
    <header>
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">H</div>
        <div>
          <p class="eyebrow">Halden · Østfold</p>
          <h1>Halden videregående skole</h1>
        </div>
      </div>
      <div class="clock" aria-label="Klokke og dato">
        <div id="clock">--:--</div>
        <div id="date">Henter dato …</div>
      </div>
      <nav class="site-nav" aria-label="Hovedmeny">
        <a href="/byer">Været i flere byer</a>
      </nav>
    </header>
    <main>
      <section class="card weather-card" aria-labelledby="weather-title">
        <div class="section-heading">
          <h2 id="weather-title">Været i Halden</h2>
          <span class="live">Oppdateres automatisk</span>
        </div>
        <div id="weather-content" class="message" aria-live="polite">
          <div><strong>Henter værmeldingen</strong>Et øyeblikk …</div>
        </div>
      </section>
      <section class="card bus-card" aria-labelledby="bus-title">
        <div class="section-heading bus-heading">
          <div>
            <h2 id="bus-title">Bussavganger</h2>
            <p class="bus-subtitle">Holdeplasser nær Halden videregående skole</p>
          </div>
          <span class="bus-badge">ENTUR · LIVE</span>
        </div>
        <div class="table-head" aria-hidden="true">
          <span>Avgang</span><span>Linje</span><span>Destinasjon</span><span>Holdeplass</span>
        </div>
        <div id="departures" aria-live="polite">
          <div class="message"><div><strong>Henter bussavganger</strong>Et øyeblikk …</div></div>
        </div>
      </section>
    </main>
    <footer>
      <span>Værdata: <a href="https://www.yr.no/" target="_blank" rel="noreferrer">Yr / MET Norway</a> · Rutetider: <a href="https://entur.no/" target="_blank" rel="noreferrer">Entur</a></span>
      <span id="status">Kobler til tjenestene …</span>
    </footer>
  </div>
  <script>
    const weatherNames = {
      clearsky: "Klarvær", fair: "Lettskyet", partlycloudy: "Delvis skyet",
      cloudy: "Skyet", fog: "Tåke", lightrain: "Lett regn", rain: "Regn",
      heavyrain: "Kraftig regn", lightrainshowers: "Lette regnbyger",
      rainshowers: "Regnbyger", heavyrainshowers: "Kraftige regnbyger",
      lightsleet: "Lett sludd", sleet: "Sludd", heavysleet: "Kraftig sludd",
      lightsnow: "Lett snø", snow: "Snø", heavysnow: "Kraftig snø",
      lightsnowshowers: "Lette snøbyger", snowshowers: "Snøbyger",
      heavysnowshowers: "Kraftige snøbyger", rainandthunder: "Regn og torden"
    };
    const weatherIcons = {
      clearsky: "☀️", fair: "🌤️", partlycloudy: "⛅", cloudy: "☁️",
      fog: "🌫️", lightrain: "🌦️", rain: "🌧️", heavyrain: "🌧️",
      lightrainshowers: "🌦️", rainshowers: "🌦️", heavyrainshowers: "🌧️",
      lightsleet: "🌨️", sleet: "🌨️", heavysleet: "🌨️",
      lightsnow: "🌨️", snow: "❄️", heavysnow: "❄️",
      lightsnowshowers: "🌨️", snowshowers: "🌨️", heavysnowshowers: "❄️",
      rainandthunder: "⛈️"
    };
    const symbolKey = symbol => (symbol || "cloudy").replace(/_.*$/, "").toLowerCase();
    const iconFor = symbol => weatherIcons[symbolKey(symbol)] || "🌥️";
    const nameFor = symbol => weatherNames[symbolKey(symbol)] || "Værmelding";
    const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
    const number = (value, decimals = 0) => Number.isFinite(value)
      ? new Intl.NumberFormat("no-NO", { maximumFractionDigits: decimals }).format(value)
      : "–";
    const clock = new Intl.DateTimeFormat("no-NO", { hour: "2-digit", minute: "2-digit" });
    const date = new Intl.DateTimeFormat("no-NO", {
      weekday: "long", day: "numeric", month: "long"
    });

    function updateClock() {
      const now = new Date();
      document.getElementById("clock").textContent = clock.format(now);
      document.getElementById("date").textContent = date.format(now);
    }

    function renderWeather(weather, error) {
      const container = document.getElementById("weather-content");
      if (!weather || !weather.current) {
        container.innerHTML = `<div><strong>Været er ikke tilgjengelig</strong>${escapeHtml(error || "Prøv igjen om litt.")}</div>`;
        return;
      }
      const current = weather.current;
      const forecasts = weather.forecasts || [];
      container.className = "";
      container.innerHTML = `
        <div class="weather-now">
          <div>
            <div class="temperature">${number(current.temperature)}°</div>
            <p class="condition">${escapeHtml(nameFor(current.symbol))}</p>
          </div>
          <div class="weather-icon" aria-hidden="true">${iconFor(current.symbol)}</div>
        </div>
        <div class="weather-details">
          <div class="detail"><div class="detail-label">Vind</div><div class="detail-value">${number(current.wind, 1)} <small>m/s</small></div></div>
          <div class="detail"><div class="detail-label">Nedbør neste time</div><div class="detail-value">${number(current.precipitation, 1)} <small>mm</small></div></div>
        </div>
        <div class="forecast">
          <p class="forecast-title">De neste timene</p>
          <div class="forecast-list">
            ${forecasts.map(item => `
              <div class="forecast-item">
                <div class="forecast-time">${clock.format(new Date(item.time))}</div>
                <div class="forecast-icon" aria-hidden="true">${iconFor(item.symbol)}</div>
                <div class="forecast-temp">${number(item.temperature)}°</div>
              </div>`).join("")}
          </div>
        </div>`;
    }

    function renderDepartures(departures, error) {
      const container = document.getElementById("departures");
      if (!departures || departures.length === 0) {
        container.innerHTML = `<div class="message"><div><strong>${error ? "Bussavganger er ikke tilgjengelige" : "Ingen avganger akkurat nå"}</strong>${escapeHtml(error || "Nye avganger vises når de nærmer seg.")}</div></div>`;
        return;
      }
      const now = Date.now();
      container.innerHTML = departures.map(item => {
        const time = new Date(item.time);
        const minutes = Math.max(0, Math.round((time.getTime() - now) / 60000));
        const scheduled = item.scheduledTime && item.scheduledTime !== item.time
          ? `<span class="departure-min">Rute ${clock.format(new Date(item.scheduledTime))}</span>`
          : `<span class="departure-min">${minutes === 0 ? "nå" : `om ${minutes} min`}</span>`;
        const distance = item.distance == null ? "" : `${number(item.distance)} m`;
        return `<div class="departure">
          <div class="departure-time">${clock.format(time)}${scheduled}</div>
          <div><span class="line">${escapeHtml(item.line)}</span></div>
          <div><div class="destination">${escapeHtml(item.destination)}</div><div class="stop">${escapeHtml(item.stop)}</div></div>
          <div class="distance">${distance}</div>
        </div>`;
      }).join("");
    }

    async function refresh() {
      try {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        if (!response.ok) throw new Error(`Serveren svarte ${response.status}.`);
        const data = await response.json();
        renderWeather(data.weather, data.errors.weather);
        renderDepartures(data.departures, data.errors.departures);
        document.getElementById("status").textContent =
          `Sist oppdatert ${clock.format(new Date(data.updatedAt))}`;
      } catch (error) {
        document.getElementById("status").textContent = "Kan ikke hente opplysninger fra serveren.";
        renderWeather(null, error.message);
        renderDepartures([], error.message);
      }
    }

    updateClock();
    refresh();
    setInterval(updateClock, 1000);
    setInterval(refresh, 60_000);
  </script>
</body>
</html>
"""


CITY_WEATHER_PAGE = r"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101b2d">
  <title>Været i flere byer · Yr</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: "Segoe UI", Arial, sans-serif;
      background: #142a39;
      color: #f4fbfb;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
    }
    * { box-sizing: border-box; }
    body {
      min-width: 320px;
      min-height: 100vh;
      margin: 0;
      background:
        radial-gradient(ellipse at 12% 0%, #316777 0, transparent 42%),
        radial-gradient(ellipse at 100% 100%, #236e70 0, transparent 38%),
        linear-gradient(135deg, #193849, #173b45 55%, #19434a);
    }
    .page { width: min(1540px, 100%); margin: 0 auto; padding: 38px 48px 24px; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 24px; margin-bottom: 36px; }
    .brand { display: flex; align-items: center; gap: 16px; }
    .brand-mark {
      display: grid; width: 52px; height: 52px; place-items: center;
      border: 1px solid #c1fff145; border-radius: 17px;
      background: linear-gradient(145deg, #70ddcf, #329eaa);
      box-shadow: 0 8px 26px #36c5b855; font-size: 25px;
    }
    .eyebrow { margin: 0 0 5px; color: #b4d9d9; font-size: 12px; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; }
    h1 { margin: 0; font-size: clamp(24px, 3vw, 37px); letter-spacing: -.04em; }
    .intro { margin: 9px 0 0; color: #b8d1d6; font-size: 15px; }
    .header-actions { display: flex; align-items: center; gap: 24px; }
    .clock { min-width: 104px; text-align: right; }
    #clock { font-size: 31px; font-weight: 650; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
    .clock-label { margin-top: 2px; color: #b8d1d6; font-size: 11px; }
    .nav-link { padding: 10px 14px; border: 1px solid #a9eeee37; border-radius: 12px; background: #c8ffff0a; color: #e1fbf7; font-size: 13px; text-decoration: none; white-space: nowrap; }
    .nav-link:hover { background: #c8ffff18; }
    .status { display: flex; justify-content: space-between; align-items: center; gap: 15px; margin: 23px 0 16px; color: #b2cccf; font-size: 12px; }
    .status-label { display: inline-flex; align-items: center; gap: 8px; }
    .status-label::before { width: 7px; height: 7px; border-radius: 50%; background: #72e7cf; box-shadow: 0 0 12px #72e7cf; content: ""; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
    .card { overflow: hidden; min-height: 307px; padding: 23px; border: 1px solid #c8ffff24; border-radius: 22px; background: #214653d9; box-shadow: 0 24px 70px #081c253b; backdrop-filter: blur(12px); }
    .card-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    h2 { margin: 0; font-size: 21px; letter-spacing: -.03em; }
    .country { margin-top: 4px; color: #b1ced0; font-size: 12px; }
    .now { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin: 25px 0 20px; }
    .temp { font-size: clamp(56px, 7vw, 78px); line-height: .96; font-weight: 550; letter-spacing: -.08em; font-variant-numeric: tabular-nums; }
    .condition { margin: 9px 0 0 4px; color: #c0dcdf; font-size: 14px; }
    .icon { padding-right: 6px; font-size: 54px; filter: drop-shadow(0 8px 18px #f4c66b35); }
    .details { display: flex; gap: 10px; }
    .detail { flex: 1; padding: 11px 13px; border: 1px solid #c8ffff20; border-radius: 13px; background: #c8ffff0a; }
    .detail-label { color: #b1ced0; font-size: 11px; }
    .detail-value { margin-top: 5px; font-size: 16px; font-weight: 650; }
    .forecast { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 5px; margin-top: 15px; }
    .forecast-item { padding: 8px 3px; border-radius: 10px; background: #c8ffff0a; text-align: center; }
    .forecast-time { color: #b1ced0; font-size: 10px; }
    .forecast-icon { margin: 6px 0; font-size: 16px; }
    .forecast-temp { font-size: 12px; font-weight: 700; }
    .message { display: grid; min-height: 210px; padding: 25px; place-content: center; border: 1px solid #c8ffff24; border-radius: 22px; background: #214653d9; color: #c0d8da; line-height: 1.6; text-align: center; }
    .message strong { display: block; margin-bottom: 6px; color: #f0fbfa; font-size: 16px; }
    footer { display: flex; justify-content: space-between; gap: 14px; margin-top: 20px; color: #a8c4c8; font-size: 11px; }
    footer a { color: #a5f2e7; text-decoration: none; }
    @media (max-width: 1050px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 650px) {
      .page { padding: 23px 15px; }
      header { align-items: flex-start; margin-bottom: 27px; }
      .brand { gap: 11px; }
      .brand-mark { width: 45px; height: 45px; border-radius: 14px; }
      .eyebrow { font-size: 10px; }
      .intro { font-size: 13px; }
      .header-actions { align-items: flex-end; flex-direction: column-reverse; gap: 10px; }
      .clock { min-width: 0; }
      #clock { font-size: 24px; }
      .clock-label { font-size: 10px; }
      .nav-link { padding: 8px 10px; font-size: 11px; }
      .grid { grid-template-columns: 1fr; gap: 12px; }
      .card { min-height: 0; padding: 20px; }
      .temp { font-size: 68px; }
      .status { align-items: flex-start; flex-direction: column; }
      footer { flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <div class="page">
    <header>
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">☁</div>
        <div>
          <p class="eyebrow">Yr · Nordisk vær</p>
          <h1>Været i flere byer</h1>
          <p class="intro">Se temperatur, vind og værmelding akkurat nå.</p>
        </div>
      </div>
      <div class="header-actions">
        <div class="clock" aria-label="Lokal tid">
          <div id="clock">--:--:--</div>
          <div class="clock-label">Norsk tid · Oslo</div>
        </div>
        <a class="nav-link" href="/">← Til informasjonsskjermen</a>
      </div>
    </header>
    <div class="status">
      <span class="status-label">Værdata fra Yr / MET Norway</span>
      <span id="updated">Henter værdata …</span>
    </div>
    <main id="cities" class="grid" aria-live="polite">
      <div class="message"><div><strong>Henter værmelding</strong>Et øyeblikk …</div></div>
    </main>
    <footer>
      <span>Værdata: <a href="https://www.yr.no/" target="_blank" rel="noreferrer">Yr / MET Norway</a></span>
      <span>Oppdateres automatisk hvert minutt</span>
    </footer>
  </div>
  <script>
    const names = {
      clearsky: "Klarvær", fair: "Lettskyet", partlycloudy: "Delvis skyet",
      cloudy: "Skyet", fog: "Tåke", lightrain: "Lett regn", rain: "Regn",
      heavyrain: "Kraftig regn", lightrainshowers: "Lette regnbyger",
      rainshowers: "Regnbyger", heavyrainshowers: "Kraftige regnbyger",
      lightsleet: "Lett sludd", sleet: "Sludd", heavysleet: "Kraftig sludd",
      lightsnow: "Lett snø", snow: "Snø", heavysnow: "Kraftig snø",
      lightsnowshowers: "Lette snøbyger", snowshowers: "Snøbyger",
      heavysnowshowers: "Kraftige snøbyger", rainandthunder: "Regn og torden"
    };
    const icons = {
      clearsky: "☀️", fair: "🌤️", partlycloudy: "⛅", cloudy: "☁️",
      fog: "🌫️", lightrain: "🌦️", rain: "🌧️", heavyrain: "🌧️",
      lightrainshowers: "🌦️", rainshowers: "🌦️", heavyrainshowers: "🌧️",
      lightsleet: "🌨️", sleet: "🌨️", heavysleet: "🌨️",
      lightsnow: "🌨️", snow: "❄️", heavysnow: "❄️",
      lightsnowshowers: "🌨️", snowshowers: "🌨️", heavysnowshowers: "❄️",
      rainandthunder: "⛈️"
    };
    const keyFor = symbol => (symbol || "cloudy").replace(/_.*$/, "").toLowerCase();
    const iconFor = symbol => icons[keyFor(symbol)] || "🌥️";
    const nameFor = symbol => names[keyFor(symbol)] || "Værmelding";
    const esc = value => String(value).replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
    const num = value => Number.isFinite(value)
      ? new Intl.NumberFormat("no-NO", { maximumFractionDigits: 0 }).format(value) : "–";
    const oneDecimal = value => Number.isFinite(value)
      ? new Intl.NumberFormat("no-NO", { maximumFractionDigits: 1 }).format(value) : "–";
    const time = new Intl.DateTimeFormat("no-NO", { hour: "2-digit", minute: "2-digit" });
    const updatedTime = new Intl.DateTimeFormat("no-NO", {
      hour: "2-digit", minute: "2-digit"
    });
    const localClock = new Intl.DateTimeFormat("no-NO", {
      timeZone: "Europe/Oslo", hour: "2-digit", minute: "2-digit", second: "2-digit"
    });

    function updateClock() {
      document.getElementById("clock").textContent = localClock.format(new Date());
    }

    function renderCity(city) {
      if (city.error || !city.current) {
        return `<article class="card"><div class="card-header"><div><h2>${esc(city.name)}</h2><div class="country">Yr-varsel</div></div></div><div class="message"><div><strong>Været er ikke tilgjengelig</strong>${esc(city.error || "Prøv igjen om litt.")}</div></div></article>`;
      }
      const current = city.current;
      const forecast = city.forecasts || [];
      return `<article class="card">
        <div class="card-header">
          <div><h2>${esc(city.name)}</h2><div class="country">Værmelding fra Yr</div></div>
        </div>
        <div class="now">
          <div><div class="temp">${num(current.temperature)}°</div><p class="condition">${esc(nameFor(current.symbol))}</p></div>
          <div class="icon" aria-hidden="true">${iconFor(current.symbol)}</div>
        </div>
        <div class="details">
          <div class="detail"><div class="detail-label">Vind</div><div class="detail-value">${oneDecimal(current.wind)} m/s</div></div>
          <div class="detail"><div class="detail-label">Nedbør neste time</div><div class="detail-value">${oneDecimal(current.precipitation)} mm</div></div>
        </div>
        <div class="forecast">
          ${forecast.map(item => `<div class="forecast-item">
            <div class="forecast-time">${time.format(new Date(item.time))}</div>
            <div class="forecast-icon" aria-hidden="true">${iconFor(item.symbol)}</div>
            <div class="forecast-temp">${num(item.temperature)}°</div>
          </div>`).join("")}
        </div>
      </article>`;
    }

    async function refresh() {
      try {
        const response = await fetch("/api/byer", { cache: "no-store" });
        if (!response.ok) throw new Error(`Serveren svarte ${response.status}.`);
        const data = await response.json();
        document.getElementById("cities").innerHTML = data.cities.map(renderCity).join("");
        document.getElementById("updated").textContent =
          `Sist oppdatert ${updatedTime.format(new Date(data.updatedAt))}`;
      } catch (error) {
        document.getElementById("cities").innerHTML =
          `<div class="message"><div><strong>Værmeldingen kunne ikke lastes</strong>${esc(error.message)}</div></div>`;
        document.getElementById("updated").textContent = "Kontroller tilkoblingen og prøv igjen.";
      }
    }

    updateClock();
    refresh();
    setInterval(updateClock, 1000);
    setInterval(refresh, 60_000);
  </script>
</body>
</html>
"""


class InformationScreenHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            content = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path == "/byer":
            content = CITY_WEATHER_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path == "/api/dashboard":
            content = json.dumps(dashboard_data(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path == "/api/byer":
            content = json.dumps(cities_weather_data(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, "Fant ikke siden.")

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), InformationScreenHandler)
    print(f"Informasjonsskjermen er klar: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServeren stoppes.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
