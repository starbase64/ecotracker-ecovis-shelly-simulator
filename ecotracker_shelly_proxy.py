#!/usr/bin/env python3
"""Gedaempfte EcoTracker-Bridge fuer den simulierten Shelly Pro 3EM.

Der ECOVIS interpretiert den gemeldeten Netzwert als wiederholte Korrektur
seiner Ausgangsleistung, nicht als absoluten Sollwert. Deshalb meldet diese
Bridge nur einen konfigurierbaren Anteil des echten Netzwerts. Sie begrenzt
ausdruecklich NICHT die Ausgangsleistung des ECOVIS.

Nur Python-Standardbibliothek; geeignet fuer python:3.13-alpine.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "4.0.0"


def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        print(f"[warn] {name}='{raw}' ist keine Zahl, nutze {default}", flush=True)
        return float(default)
    if not math.isfinite(value):
        print(f"[warn] {name}='{raw}' ist nicht endlich, nutze {default}", flush=True)
        return float(default)
    return value


class Config:
    ECOTRACKER_URL = env_str("ECOTRACKER_URL", "http://192.168.1.50:18080/v1/json")
    HTTP_TIMEOUT = env_float("HTTP_TIMEOUT", 1.5)
    ECOTRACKER_MAX_AGE_MS = env_float("ECOTRACKER_MAX_AGE_MS", 3000.0)

    LISTEN_HOST = env_str("LISTEN_HOST", "127.0.0.1")
    LISTEN_PORT = int(env_float("LISTEN_PORT", 18081))
    POLL_INTERVAL = env_float("POLL_INTERVAL", 1.0)
    LOG_INTERVAL = env_float("LOG_INTERVAL", 5.0)

    GRID_SMOOTH_S = env_float("GRID_SMOOTH_S", 3.0)
    REPORT_GAIN_UP = env_float("REPORT_GAIN_UP", 0.4)
    REPORT_GAIN_DOWN = env_float("REPORT_GAIN_DOWN", 0.6)
    REPORT_DEADBAND_W = env_float("REPORT_DEADBAND_W", 10.0)
    REPORT_HOLD_S = env_float("REPORT_HOLD_S", 5.0)
    FAST_EXPORT_W = env_float("FAST_EXPORT_W", 30.0)

    # Grenzen eines einzelnen Korrektursignals. Das sind KEINE Grenzen der
    # Ausgangsleistung des ECOVIS.
    MAX_CORRECTION_UP_W = env_float("MAX_CORRECTION_UP_W", 800.0)
    MAX_CORRECTION_DOWN_W = env_float("MAX_CORRECTION_DOWN_W", 2000.0)

    STALE_S = env_float("STALE_S", 5.0)
    STALE_HARD_S = env_float("STALE_HARD_S", 15.0)
    SAFE_REDUCE_W = env_float("SAFE_REDUCE_W", -300.0)
    SAFE_REDUCE_HARD_W = env_float("SAFE_REDUCE_HARD_W", -800.0)


def validate_config() -> None:
    checks = (
        (0.0 <= Config.REPORT_GAIN_UP <= 1.0, "REPORT_GAIN_UP muss zwischen 0 und 1 liegen"),
        (0.0 <= Config.REPORT_GAIN_DOWN <= 1.0, "REPORT_GAIN_DOWN muss zwischen 0 und 1 liegen"),
        (Config.REPORT_DEADBAND_W >= 0.0, "REPORT_DEADBAND_W darf nicht negativ sein"),
        (Config.REPORT_HOLD_S >= 0.0, "REPORT_HOLD_S darf nicht negativ sein"),
        (Config.POLL_INTERVAL > 0.0, "POLL_INTERVAL muss positiv sein"),
        (Config.HTTP_TIMEOUT > 0.0, "HTTP_TIMEOUT muss positiv sein"),
        (Config.MAX_CORRECTION_UP_W > 0.0, "MAX_CORRECTION_UP_W muss positiv sein"),
        (Config.MAX_CORRECTION_DOWN_W > 0.0,
         "MAX_CORRECTION_DOWN_W muss positiv sein"),
        (Config.STALE_S > 0.0, "STALE_S muss positiv sein"),
        (Config.STALE_HARD_S >= Config.STALE_S, "STALE_HARD_S muss >= STALE_S sein"),
        (Config.SAFE_REDUCE_W <= 0.0, "SAFE_REDUCE_W darf nicht positiv sein"),
        (Config.SAFE_REDUCE_HARD_W <= Config.SAFE_REDUCE_W,
         "SAFE_REDUCE_HARD_W muss mindestens so stark absenken wie SAFE_REDUCE_W"),
    )
    errors = [message for valid, message in checks if not valid]
    if errors:
        raise ValueError("; ".join(errors))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fetch_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_ecotracker(data: dict) -> tuple[float, dict]:
    """Validiert eine EcoTracker-Antwort und normalisiert optionale Felder."""
    power = float(data["power"])
    if not math.isfinite(power):
        raise ValueError("EcoTracker-Leistung ist nicht endlich")
    age_available = data.get("agePower") is not None
    age_power = float(data["agePower"]) if age_available else None
    if age_power is not None:
        if not math.isfinite(age_power) or age_power < 0:
            raise ValueError("agePower ist ungueltig")
        if age_power > Config.ECOTRACKER_MAX_AGE_MS:
            raise RuntimeError(
                "EcoTracker-Leistungswert ist {:.0f} ms alt (Grenze {:.0f} ms)".format(
                    age_power, Config.ECOTRACKER_MAX_AGE_MS))
    return power, {
        "energyCounterIn": float(data.get("energyCounterIn", 0.0) or 0.0),
        "energyCounterOut": float(data.get("energyCounterOut", 0.0) or 0.0),
        "agePower": age_power,
        "agePowerAvailable": age_available,
    }


class Reading:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: float | None = None
        self.extra: dict = {}
        self.timestamp = 0.0
        self.errors = 0

    def set(self, value: float, extra: dict | None = None) -> None:
        if not math.isfinite(value):
            raise ValueError("Messwert ist nicht endlich")
        with self.lock:
            self.value = value
            self.extra = extra or {}
            self.timestamp = time.monotonic()
            self.errors = 0

    def fail(self) -> None:
        with self.lock:
            self.errors += 1

    def get(self) -> tuple[float | None, float, dict, int]:
        with self.lock:
            age = time.monotonic() - self.timestamp if self.timestamp else 1e9
            return self.value, age, dict(self.extra), self.errors


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.payload = {
            "power": 0.0,
            "powerPhase1": 0.0,
            "powerPhase2": 0.0,
            "powerPhase3": 0.0,
            "energyCounterIn": 0.0,
            "energyCounterOut": 0.0,
            "proxyVersion": VERSION,
            "proxyState": "starting",
            "dataHealthy": False,
        }

    def publish(self, payload: dict) -> None:
        with self.lock:
            self.payload = payload

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.payload)


GRID = Reading()
STATE = State()
SHUTDOWN = threading.Event()


def poll_grid() -> None:
    age_missing_warned = False
    while not SHUTDOWN.is_set():
        try:
            data = fetch_json(Config.ECOTRACKER_URL, Config.HTTP_TIMEOUT)
            power, extra = parse_ecotracker(data)
            age_available = extra["agePowerAvailable"]
            if not age_available and not age_missing_warned:
                print("[warn] EcoTracker liefert kein agePower; pruefe nur das HTTP-Antwortalter",
                      flush=True)
                age_missing_warned = True
            elif age_available and age_missing_warned:
                print("[info] EcoTracker liefert agePower wieder", flush=True)
                age_missing_warned = False

            GRID.set(power, extra)
        except Exception as exc:  # noqa: BLE001
            GRID.fail()
            _, _, _, errors = GRID.get()
            if errors in (1, 5) or errors % 30 == 0:
                print(f"[warn] EcoTracker nicht lesbar ({errors}x): {exc}", flush=True)
        SHUTDOWN.wait(Config.POLL_INTERVAL)


class DampedProxy:
    def __init__(self) -> None:
        self.last_log = 0.0
        self.held = 0.0
        self.held_since: float | None = None
        self.smooth: float | None = None
        self.last_smooth: float | None = None

    @staticmethod
    def damp(value: float) -> tuple[float, float, str, str]:
        if abs(value) < Config.REPORT_DEADBAND_W:
            return 0.0, 0.0, "hold", "deadband"
        if value > 0:
            gain, direction, state = Config.REPORT_GAIN_UP, "raise", "damped_raise"
        else:
            gain, direction, state = Config.REPORT_GAIN_DOWN, "reduce", "damped_reduce"
        reported = clamp(value * gain,
                         -Config.MAX_CORRECTION_DOWN_W,
                         Config.MAX_CORRECTION_UP_W)
        return reported, gain, direction, state

    def step(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        grid_raw, grid_age, extra, _ = GRID.get()
        grid_ok = grid_raw is not None and grid_age < Config.STALE_S

        if grid_ok:
            smoothed = self.smoothed(float(grid_raw), now)
            desired, gain, direction, state = self.damp(smoothed)
        else:
            smoothed = None
            hard = grid_age > Config.STALE_HARD_S
            desired = Config.SAFE_REDUCE_HARD_W if hard else Config.SAFE_REDUCE_W
            gain, direction = 1.0, "reduce_safety"
            state = "failsafe_hard" if hard else "failsafe_soft"

        first = self.held_since is None
        due = first or (now - self.held_since) >= Config.REPORT_HOLD_S
        urgent_export = (grid_ok and float(grid_raw) < -Config.FAST_EXPORT_W
                         and desired < self.held)
        failsafe = state.startswith("failsafe")
        if due or urgent_export or failsafe:
            self.held = desired
            self.held_since = now
        else:
            state += "_hold"

        return self.payload(
            reported=self.held,
            state=state,
            grid_raw=grid_raw,
            grid_smoothed=smoothed,
            grid_age=grid_age,
            extra=extra,
            gain=gain,
            direction=direction,
            healthy=grid_ok,
        )

    def smoothed(self, value: float, now: float) -> float:
        if Config.GRID_SMOOTH_S <= 0:
            return value
        if self.smooth is None or self.last_smooth is None:
            self.smooth, self.last_smooth = value, now
            return value
        if value < -Config.FAST_EXPORT_W:
            self.smooth, self.last_smooth = value, now
            return value
        elapsed = max(0.0, now - self.last_smooth)
        alpha = 1.0 - math.exp(-elapsed / Config.GRID_SMOOTH_S) if elapsed else 0.0
        self.smooth += alpha * (value - self.smooth)
        self.last_smooth = now
        return self.smooth

    @staticmethod
    def payload(*, reported: float, state: str, grid_raw: float | None,
                grid_smoothed: float | None, grid_age: float, extra: dict,
                gain: float, direction: str, healthy: bool) -> dict:
        reported = round(float(reported), 1)
        third = round(reported / 3.0, 2)
        result = {
            "power": reported,
            "powerPhase1": third,
            "powerPhase2": third,
            "powerPhase3": third,
            "energyCounterIn": extra.get("energyCounterIn", 0.0),
            "energyCounterOut": extra.get("energyCounterOut", 0.0),
            "proxyVersion": VERSION,
            "proxyState": state,
            "gridPowerRaw": None if grid_raw is None else round(float(grid_raw), 1),
            "gridPowerSmoothed": (None if grid_smoothed is None
                                  else round(float(grid_smoothed), 1)),
            "reportedPower": reported,
            "reportGain": gain,
            "reportDirection": direction,
            "dataHealthy": healthy,
            "dataAgeSeconds": round(grid_age, 2),
            "ecoTrackerAgePowerMs": extra.get("agePower"),
            "ecoTrackerAgePowerAvailable": bool(extra.get("agePowerAvailable", False)),
            "limiterVersion": VERSION,
            "limiterState": state,
            "limiterGridPower": None if grid_raw is None else round(float(grid_raw), 1),
            "limiterGridSmoothed": (None if grid_smoothed is None
                                    else round(float(grid_smoothed), 1)),
            "limiterReported": reported,
            "limiterEcoTrackerAgeMs": extra.get("agePower"),
        }
        return result

    def maybe_log(self, payload: dict) -> None:
        now = time.monotonic()
        if now - self.last_log < Config.LOG_INTERVAL:
            return
        self.last_log = now

        def fmt(value) -> str:
            return "   --  " if value is None else f"{value:7.1f}"

        print("Netz={} | geglaettet={} | Faktor={:.2f} | gemeldet={} | {}".format(
            fmt(payload["gridPowerRaw"]), fmt(payload["gridPowerSmoothed"]),
            payload["reportGain"], fmt(payload["reportedPower"]),
            payload["proxyState"]), flush=True)


def control_loop() -> None:
    proxy = DampedProxy()
    next_tick = time.monotonic()
    while not SHUTDOWN.is_set():
        payload = proxy.step()
        STATE.publish(payload)
        proxy.maybe_log(payload)
        next_tick += Config.POLL_INTERVAL
        sleep_for = next_tick - time.monotonic()
        if sleep_for < 0:
            next_tick = time.monotonic()
            sleep_for = 0.0
        SHUTDOWN.wait(sleep_for)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("", "/v1/json", "/json", "/healthz"):
            self.send_error(404)
            return
        snapshot = STATE.snapshot()
        body_data = ({"service": "ok", "version": VERSION,
                      "dataHealthy": snapshot.get("dataHealthy", False)}
                     if path == "/healthz" else snapshot)
        body = json.dumps(body_data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def main() -> int:
    validate_config()

    def stop(*_args) -> None:
        print("[info] Beende EcoTracker-Shelly-Proxy", flush=True)
        SHUTDOWN.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print("[info] EcoTracker-Shelly-Proxy {} | Gain hoch {:.2f} | "
          "Gain runter {:.2f} | Haltezeit {:.1f}s".format(
              VERSION, Config.REPORT_GAIN_UP, Config.REPORT_GAIN_DOWN,
              Config.REPORT_HOLD_S), flush=True)
    print("[warn] Diese Bridge begrenzt NICHT die Ausgangsleistung des ECOVIS", flush=True)

    server = ThreadingHTTPServer((Config.LISTEN_HOST, Config.LISTEN_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[info] Proxy laeuft auf http://{Config.LISTEN_HOST}:"
          f"{Config.LISTEN_PORT}/v1/json", flush=True)

    threading.Thread(target=poll_grid, daemon=True).start()
    control_loop()
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
