#!/usr/bin/env python3
"""
EcoTracker -> ECO-WORTHY ECOVIS Limiter (schwingungsarme Variante)

Liest die Netzleistung vom EcoTracker und die tatsaechliche AC-Ausgangsleistung
des ECOVIS von einem Shelly. Daraus wird ein gedaempfter Korrekturwert berechnet
und im EcoTracker-JSON-Format bereitgestellt. uni-meter liest diese URL und
simuliert daraus einen Shelly Pro 3EM, den der ECOVIS abfragt.

Nur Standardbibliothek - laeuft unveraendert in python:3.13-alpine.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.1.1"


# --------------------------------------------------------------------------
# Konfiguration (alles per Umgebungsvariable ueberschreibbar)
# --------------------------------------------------------------------------

def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        print(f"[warn] {name}='{raw}' ist keine Zahl, nutze {default}", flush=True)
        return float(default)


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "ja")


class Config:
    # --- Datenquellen -----------------------------------------------------
    ECOTRACKER_URL = env_str("ECOTRACKER_URL", "http://192.168.1.50:18080/v1/json")
    INVERTER_URL = env_str("INVERTER_URL", "http://192.168.1.51/rpc/Switch.GetStatus?id=0")
    # Punkt-Pfad in der JSON-Antwort, z.B. "apower" (Gen2/3) oder "meters.0.power" (Gen1)
    INVERTER_FIELD = env_str("INVERTER_FIELD", "apower")
    INVERTER_INVERT = env_bool("INVERTER_INVERT", True)
    HTTP_TIMEOUT = env_float("HTTP_TIMEOUT", 1.5)
    ECOTRACKER_MAX_AGE_MS = env_float("ECOTRACKER_MAX_AGE_MS", 5000.0)

    # --- eigener HTTP-Endpunkt fuer uni-meter -----------------------------
    LISTEN_HOST = env_str("LISTEN_HOST", "127.0.0.1")
    LISTEN_PORT = int(env_float("LISTEN_PORT", 18081))

    # --- Takt -------------------------------------------------------------
    POLL_INTERVAL = env_float("POLL_INTERVAL", 1.0)
    LOG_INTERVAL = env_float("LOG_INTERVAL", 5.0)

    # --- Leistungsgrenzen -------------------------------------------------
    MAX_OUTPUT_W = env_float("MAX_OUTPUT_W", 740.0)      # Sollwert-Deckel
    HARD_LIMIT_W = env_float("HARD_LIMIT_W", 790.0)      # ab hier Notzweig
    HARD_GAIN = env_float("HARD_GAIN", 2.0)
    HARD_MIN_PUSH_W = env_float("HARD_MIN_PUSH_W", 30.0)

    # --- Reglerparameter --------------------------------------------------
    DEADBAND_W = env_float("DEADBAND_W", 15.0)
    KP_UP = env_float("KP_UP", 0.40)                     # Schleifenverstaerkung Anheben
    KP_DOWN = env_float("KP_DOWN", 0.70)                 # Schleifenverstaerkung Absenken
    MAX_RAISE_SIGNAL_W = env_float("MAX_RAISE_SIGNAL_W", 200.0)
    MAX_REDUCE_SIGNAL_W = env_float("MAX_REDUCE_SIGNAL_W", 800.0)
    SETTLE_S = env_float("SETTLE_S", 3.0)                # gemessene Totzeit der Strecke

    # --- Glaettung --------------------------------------------------------
    GRID_EMA_ALPHA = env_float("GRID_EMA_ALPHA", 0.6)
    INV_EMA_ALPHA = env_float("INV_EMA_ALPHA", 0.6)

    # --- Ausfallverhalten -------------------------------------------------
    STALE_S = env_float("STALE_S", 5.0)
    STALE_HARD_S = env_float("STALE_HARD_S", 15.0)
    SAFE_CONTROL_W = env_float("SAFE_CONTROL_W", -300.0)

    # --- Betriebsart ------------------------------------------------------
    ENABLED = env_bool("ENABLED", True)


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def dig(data, path: str):
    """Holt einen Wert ueber einen Punkt-Pfad, z.B. 'meters.0.power'."""
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def fetch_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class Ema:
    """Exponentieller Glaetter. alpha nahe 1.0 = wenig Glaettung, wenig Verzug."""

    def __init__(self, alpha: float):
        self.alpha = clamp(alpha, 0.05, 1.0)
        self.value: float | None = None

    def update(self, sample: float) -> float:
        if self.value is None:
            self.value = sample
        else:
            self.value = self.alpha * sample + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None


# --------------------------------------------------------------------------
# Gemeinsamer Zustand zwischen Regelschleife und HTTP-Server
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.payload = {
            "power": 0.0,
            "powerPhase1": 0.0,
            "powerPhase2": 0.0,
            "powerPhase3": 0.0,
            "energyCounterIn": 0.0,
            "energyCounterOut": 0.0,
            "limiterState": "starting",
        }

    def publish(self, payload: dict) -> None:
        with self.lock:
            self.payload = payload

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.payload)


STATE = State()
SHUTDOWN = threading.Event()


# --------------------------------------------------------------------------
# Poller: lesen unabhaengig voneinander, damit ein haengendes Geraet
# die Regelschleife nicht blockiert
# --------------------------------------------------------------------------

class Reading:
    def __init__(self):
        self.lock = threading.Lock()
        self.value: float | None = None
        self.extra: dict = {}
        self.timestamp: float = 0.0
        self.errors: int = 0

    def set(self, value: float, extra: dict | None = None) -> None:
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


GRID = Reading()
INVERTER = Reading()


def poll_grid() -> None:
    while not SHUTDOWN.is_set():
        try:
            data = fetch_json(Config.ECOTRACKER_URL, Config.HTTP_TIMEOUT)
            if "agePower" not in data:
                raise RuntimeError("EcoTracker-Antwort enthaelt kein agePower-Feld")
            age_power = float(data["agePower"])
            if age_power < 0 or age_power > Config.ECOTRACKER_MAX_AGE_MS:
                raise RuntimeError(
                    "EcoTracker-Leistungswert ist {:.0f} ms alt (Grenze {:.0f} ms)".format(
                        age_power, Config.ECOTRACKER_MAX_AGE_MS
                    )
                )
            extra = {
                "energyCounterIn": float(data.get("energyCounterIn", 0.0) or 0.0),
                "energyCounterOut": float(data.get("energyCounterOut", 0.0) or 0.0),
                "agePower": age_power,
            }
            GRID.set(float(data["power"]), extra)
        except Exception as exc:  # noqa: BLE001
            GRID.fail()
            _, _, _, errors = GRID.get()
            if errors in (1, 5) or errors % 30 == 0:
                print(f"[warn] EcoTracker nicht lesbar ({errors}x): {exc}", flush=True)
        SHUTDOWN.wait(Config.POLL_INTERVAL)


def poll_inverter() -> None:
    while not SHUTDOWN.is_set():
        try:
            data = fetch_json(Config.INVERTER_URL, Config.HTTP_TIMEOUT)
            value = float(dig(data, Config.INVERTER_FIELD))
            if Config.INVERTER_INVERT:
                value = -value
            # Vorzeichen nach optionaler Invertierung:
            # positiv = ECOVIS speist ins Haus, negativ = ECOVIS bezieht AC.
            INVERTER.set(value)
        except Exception as exc:  # noqa: BLE001
            INVERTER.fail()
            _, _, _, errors = INVERTER.get()
            if errors in (1, 5) or errors % 30 == 0:
                print(f"[warn] Shelly nicht lesbar ({errors}x): {exc}", flush=True)
        SHUTDOWN.wait(Config.POLL_INTERVAL)


# --------------------------------------------------------------------------
# Regelschleife
# --------------------------------------------------------------------------

class Controller:
    def __init__(self):
        self.grid_ema = Ema(Config.GRID_EMA_ALPHA)
        self.inv_ema = Ema(Config.INV_EMA_ALPHA)
        self.last_log = 0.0
        self.overshoot_hits = 0
        self.was_failsafe = True

    def step(self) -> dict:
        grid_raw, grid_age, grid_extra, _ = GRID.get()
        inv_flow_raw, inv_age, _, _ = INVERTER.get()

        grid_ok = grid_raw is not None and grid_age < Config.STALE_S
        inv_ok = inv_flow_raw is not None and inv_age < Config.STALE_S
        worst_age = max(grid_age, inv_age)

        counters = {
            "energyCounterIn": grid_extra.get("energyCounterIn", 0.0),
            "energyCounterOut": grid_extra.get("energyCounterOut", 0.0),
            "agePower": grid_extra.get("agePower"),
        }

        # --- Ausfall: Wechselrichter herunterfahren lassen -----------------
        if not grid_ok or not inv_ok:
            self.was_failsafe = True
            if worst_age > Config.STALE_HARD_S:
                control = -Config.MAX_REDUCE_SIGNAL_W
                state = "failsafe_hard"
            else:
                control = Config.SAFE_CONTROL_W
                state = "failsafe"
            return self._payload(control, state, counters,
                                 grid=grid_raw,
                                 inverter=None if inv_flow_raw is None else max(0.0, inv_flow_raw),
                                 inverter_flow=inv_flow_raw,
                                 house=None, target=None, age=worst_age)

        # Nach einem Messausfall nicht mit alten geglaetteten Werten
        # weiterregeln. Der erste gueltige Messsatz bildet den Neustartpunkt.
        if self.was_failsafe:
            self.grid_ema.reset()
            self.inv_ema.reset()
            self.was_failsafe = False

        grid_f = self.grid_ema.update(grid_raw)
        inv_flow_f = self.inv_ema.update(inv_flow_raw)
        inv_raw = max(0.0, inv_flow_raw)
        inv_f = max(0.0, inv_flow_f)
        # Der signierte AC-Fluss gehoert in die Hauslastbilanz. Dadurch wird
        # Bezug des ECOVIS nicht faelschlich als zusaetzliche Hauslast gewertet.
        house = grid_f + inv_flow_f
        target = clamp(house, 0.0, Config.MAX_OUTPUT_W)

        # --- Durchleitbetrieb ohne Begrenzung -----------------------------
        if not Config.ENABLED:
            return self._payload(grid_raw, "passthrough", counters,
                                 grid=grid_raw, inverter=inv_raw,
                                 inverter_flow=inv_flow_raw,
                                 house=house, target=None, age=worst_age)

        # --- Notzweig: Ausgangsleistung zu hoch ---------------------------
        # Umgeht Daempfung und Totband. Nutzt den ungeglaetteten Messwert,
        # damit er so frueh wie moeglich greift.
        if inv_raw > Config.HARD_LIMIT_W:
            ticks = max(1.0, Config.SETTLE_S / max(0.1, Config.POLL_INTERVAL))
            push = (inv_raw - Config.MAX_OUTPUT_W) * Config.HARD_GAIN / (ticks + 1.0)
            control = -min(max(push, Config.HARD_MIN_PUSH_W),
                           Config.MAX_REDUCE_SIGNAL_W)
            self.overshoot_hits += 1
            if self.overshoot_hits in (10, 50) or self.overshoot_hits % 200 == 0:
                print(
                    "[warn] Notzweig schon {}x aktiv - SETTLE_S ({:.1f}s) ist "
                    "vermutlich kleiner als die echte Totzeit. Siehe README, "
                    "Abschnitt 'Einmessen'.".format(
                        self.overshoot_hits, Config.SETTLE_S),
                    flush=True,
                )
            return self._payload(control, "overshoot", counters,
                                 grid=grid_raw, inverter=inv_raw,
                                 inverter_flow=inv_flow_raw,
                                 house=house, target=target, age=worst_age)

        # --- Regelkern -----------------------------------------------------
        # Der ECOVIS wirkt wie ein Integrator: er verschiebt seine Leistung
        # solange, wie ein Wert ungleich Null gemeldet wird, und waehrend der
        # Totzeit summiert er mehrere Meldungen auf. Eine Korrektur wird
        # deshalb ueber das ganze Totzeitfenster verteilt: KP_UP bzw. KP_DOWN
        # sind der Anteil der Differenz, der sich ueber SETTLE_S Sekunden
        # insgesamt summiert. Werte unter 1.0 sind Pflicht, sonst schwingt es.
        ticks = max(1.0, Config.SETTLE_S / max(0.1, Config.POLL_INTERVAL))
        error = clamp(house, 0.0, Config.MAX_OUTPUT_W) - inv_f

        if abs(error) < Config.DEADBAND_W:
            control = 0.0
            state = "hold"
        elif error > 0:
            control = error * (Config.KP_UP / (ticks + 1.0))
            state = "ceiling" if house > Config.MAX_OUTPUT_W else "raise"
        else:
            control = error * (Config.KP_DOWN / (ticks + 1.0))
            state = "reduce"

        control = clamp(control,
                        -Config.MAX_REDUCE_SIGNAL_W,
                        Config.MAX_RAISE_SIGNAL_W)

        return self._payload(control, state, counters,
                             grid=grid_raw, inverter=inv_raw,
                             inverter_flow=inv_flow_raw,
                             house=house, target=target, age=worst_age)

    @staticmethod
    def _payload(control: float, state: str, counters: dict,
                 grid, inverter, inverter_flow, house, target, age: float) -> dict:
        control = round(float(control), 1)
        third = round(control / 3.0, 2)
        return {
            # Felder, die uni-meter liest
            "power": control,
            "powerPhase1": third,
            "powerPhase2": third,
            "powerPhase3": third,
            "energyCounterIn": counters["energyCounterIn"],
            "energyCounterOut": counters["energyCounterOut"],
            # Diagnose
            "limiterVersion": VERSION,
            "limiterState": state,
            "limiterGridPower": None if grid is None else round(grid, 1),
            "limiterInverterPower": None if inverter is None else round(inverter, 1),
            "limiterInverterFlow": None if inverter_flow is None else round(inverter_flow, 1),
            "limiterHouseLoad": None if house is None else round(house, 1),
            "limiterTargetPower": None if target is None else round(target, 1),
            "limiterControlPower": control,
            "limiterDataAge": round(age, 2),
            "limiterEcoTrackerAgeMs": counters.get("agePower"),
        }

    def maybe_log(self, payload: dict) -> None:
        now = time.monotonic()
        if now - self.last_log < Config.LOG_INTERVAL:
            return
        self.last_log = now

        def fmt(value):
            return "   --  " if value is None else f"{value:7.1f}"

        print(
            "Netz={} | ECOVIS={} | Haus={} | Ziel={} | Korrektur={} | {}".format(
                fmt(payload["limiterGridPower"]),
                fmt(payload["limiterInverterPower"]),
                fmt(payload["limiterHouseLoad"]),
                fmt(payload["limiterTargetPower"]),
                fmt(payload["limiterControlPower"]),
                payload["limiterState"],
            ),
            flush=True,
        )


def control_loop() -> None:
    controller = Controller()
    next_tick = time.monotonic()
    while not SHUTDOWN.is_set():
        payload = controller.step()
        STATE.publish(payload)
        controller.maybe_log(payload)

        next_tick += Config.POLL_INTERVAL
        sleep_for = next_tick - time.monotonic()
        if sleep_for < 0:
            next_tick = time.monotonic()
            sleep_for = 0
        SHUTDOWN.wait(sleep_for)


# --------------------------------------------------------------------------
# HTTP-Endpunkt fuer uni-meter
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/v1/json", "/json"):
            body = json.dumps(STATE.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args) -> None:  # Zugriffe nicht mitloggen
        return


def serve() -> None:
    server = ThreadingHTTPServer((Config.LISTEN_HOST, Config.LISTEN_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        f"[info] Limiter laeuft auf http://{Config.LISTEN_HOST}:{Config.LISTEN_PORT}/v1/json",
        flush=True,
    )
    return server


def main() -> int:
    def stop(*_args):
        print("[info] Beende Limiter", flush=True)
        SHUTDOWN.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(
        "[info] Limiter {} | Ziel {:.0f} W | Notzweig ab {:.0f} W | KP_UP={:.2f} KP_DOWN={:.2f} "
        "| Wartepause {:.1f}s | Begrenzung {}".format(
            VERSION, Config.MAX_OUTPUT_W, Config.HARD_LIMIT_W, Config.KP_UP,
            Config.KP_DOWN, Config.SETTLE_S,
            "aktiv" if Config.ENABLED else "AUS (Durchleitbetrieb)",
        ),
        flush=True,
    )

    serve()
    threading.Thread(target=poll_grid, daemon=True).start()
    threading.Thread(target=poll_inverter, daemon=True).start()
    control_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
