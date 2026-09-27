#!/usr/bin/env python3
"""
EcoTracker -> ECO-WORTHY ECOVIS Limiter 3.0 (Spielraum-Begrenzung)

Anderer Ansatz als 2.x: Es wird kein Korrekturwert mehr berechnet, sondern der
echte Netzwert durchgereicht und nur nach oben begrenzt. Die Grenze ist der
Spielraum bis zum Deckel, geteilt durch die Zahl der gleichzeitig unterwegs
befindlichen Befehle:

    gemeldet = min( Netzwert , (CAP_W - Ausgangsleistung) / IN_FLIGHT )

Der ECOVIS verschiebt seine Leistung pro Regelzyklus um den gemeldeten Betrag
(gemessener Faktor 1.0). Die Summe aller unterwegs befindlichen Befehle kann
ihn damit rechnerisch nicht ueber CAP_W bringen. Unterhalb des Deckels bleibt
die Werksregelung mit ihrer Selbstkorrektur aktiv.

Negative Werte (Einspeisung) werden ungekuerzt durchgereicht - Absenken ist die
sichere Richtung und soll so schnell wie moeglich passieren.

Nur Standardbibliothek - laeuft unveraendert in python:3.13-alpine.
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

VERSION = "3.5.0"


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


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
    # Leer lassen = kein Shelly am Ausgang; der Limiter schaetzt dann selbst.
    INVERTER_URL = env_str("INVERTER_URL", "")
    INVERTER_FIELD = env_str("INVERTER_FIELD", "apower")
    INVERTER_INVERT = env_bool("INVERTER_INVERT", True)
    HTTP_TIMEOUT = env_float("HTTP_TIMEOUT", 1.5)
    ECOTRACKER_MAX_AGE_MS = env_float("ECOTRACKER_MAX_AGE_MS", 3000.0)

    # --- eigener HTTP-Endpunkt fuer uni-meter -----------------------------
    LISTEN_HOST = env_str("LISTEN_HOST", "127.0.0.1")
    LISTEN_PORT = int(env_float("LISTEN_PORT", 18081))

    # --- Takt -------------------------------------------------------------
    POLL_INTERVAL = env_float("POLL_INTERVAL", 1.0)
    LOG_INTERVAL = env_float("LOG_INTERVAL", 5.0)

    # --- Deckel -----------------------------------------------------------
    CAP_W = env_float("CAP_W", 760.0)            # maximale Ausgangsleistung
    IN_FLIGHT = env_float("IN_FLIGHT", 3.0)      # Befehle in der Totzeit
    # --- Schaetzung der Ausgangsleistung ohne Shelly ----------------------
    APPLY_INTERVAL_S = env_float("APPLY_INTERVAL_S", 5.0)   # Regelzyklus des ECOVIS
    DEVICE_MAX_W = env_float("DEVICE_MAX_W", 1600.0)        # Nennleistung des Geraets
    # Der gemeldete Wert wird einen ganzen Zyklus festgehalten, damit das
    # Geraet genau den Wert anwendet, den der Limiter mitzaehlt.
    FAST_EXPORT_W = env_float("FAST_EXPORT_W", 30.0)  # ab hier sofort nachfuehren
    # Ohne Messung am Ausgang kann die Schaetzung verklemmen. Steht sie am
    # Deckel, waehrend das Haus dauerhaft Strom bezieht, wird sie langsam
    # abgebaut, damit sich der Limiter neu einfangen kann.
    RESYNC_S = env_float("RESYNC_S", 60.0)
    RESYNC_W = env_float("RESYNC_W", 25.0)

    # --- Glaettung ------------------------------------------------------
    # Der ECOVIS reagiert erst nach rund 10 s und dann in 5-s-Schritten.
    # Schnellere Schwankungen kann er nicht abfangen - er jagt ihnen nur
    # hinterher. Zeitkonstante in Sekunden, 0 = aus.
    # Einseitig: Einspeisung ueber FAST_EXPORT_W geht ungeglaettet durch,
    # damit die Absenkung nicht verzoegert wird.
    GRID_SMOOTH_S = env_float("GRID_SMOOTH_S", 3.0)
    # Daempfung: der ECOVIS setzt den gemeldeten Wert mit Faktor 1.0 um, und
    # in der Totzeit sind mehrere Befehle unterwegs. Wird der volle Netzwert
    # gemeldet, korrigiert er jede Abweichung mehrfach - es entsteht ein
    # Grenzzyklus. Ein Bruchteil laesst die Abweichung geometrisch abklingen.
    REPORT_GAIN_UP = env_float("REPORT_GAIN_UP", 0.4)     # Bezug -> hochfahren
    REPORT_GAIN_DOWN = env_float("REPORT_GAIN_DOWN", 0.6)  # Einspeisung -> absenken
    # Kleine Abweichungen gar nicht erst melden - das Geraet haelt dann.
    REPORT_DEADBAND_W = env_float("REPORT_DEADBAND_W", 10.0)
    MAX_REPORT_W = env_float("MAX_REPORT_W", 800.0)   # harte Obergrenze der Meldung
    MIN_REPORT_W = env_float("MIN_REPORT_W", -2000.0)  # Untergrenze der Meldung

    # --- Ausfallverhalten -------------------------------------------------
    STALE_S = env_float("STALE_S", 5.0)
    STALE_HARD_S = env_float("STALE_HARD_S", 15.0)
    SAFE_REDUCE_W = env_float("SAFE_REDUCE_W", -300.0)
    SAFE_REDUCE_HARD_W = env_float("SAFE_REDUCE_HARD_W", -800.0)

    # --- Betriebsart ------------------------------------------------------
    ENABLED = env_bool("ENABLED", True)          # false = Deckel aus, nur durchreichen


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def dig(data, path: str):
    """Holt einen Wert ueber einen Punkt-Pfad, z.B. 'meters.0.power'."""
    current = data
    for part in path.split("."):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def fetch_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# --------------------------------------------------------------------------
# Zustand
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

    def get(self):
        with self.lock:
            age = time.monotonic() - self.timestamp if self.timestamp else 1e9
            return self.value, age, dict(self.extra), self.errors


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.payload = {
            "power": 0.0, "powerPhase1": 0.0, "powerPhase2": 0.0, "powerPhase3": 0.0,
            "energyCounterIn": 0.0, "energyCounterOut": 0.0,
            "limiterVersion": VERSION, "limiterState": "starting",
        }

    def publish(self, payload: dict) -> None:
        with self.lock:
            self.payload = payload

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.payload)


GRID = Reading()
INVERTER = Reading()
STATE = State()
SHUTDOWN = threading.Event()


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------

def poll_grid() -> None:
    while not SHUTDOWN.is_set():
        try:
            data = fetch_json(Config.ECOTRACKER_URL, Config.HTTP_TIMEOUT)
            # Der EcoTracker liefert agePower zeitweise nicht mit. Das ist kein
            # Grund fuer einen Ausfall - der Wert selbst ist ja da.
            age_power = float(data.get("agePower") or 0.0)
            if age_power > Config.ECOTRACKER_MAX_AGE_MS:
                raise RuntimeError(
                    "EcoTracker-Leistungswert ist {:.0f} ms alt (Grenze {:.0f} ms)".format(
                        age_power, Config.ECOTRACKER_MAX_AGE_MS))
            GRID.set(float(data["power"]), {
                "energyCounterIn": float(data.get("energyCounterIn", 0.0) or 0.0),
                "energyCounterOut": float(data.get("energyCounterOut", 0.0) or 0.0),
                "agePower": age_power,
            })
        except Exception as exc:  # noqa: BLE001
            GRID.fail()
            _, _, _, errors = GRID.get()
            if errors in (1, 5) or errors % 30 == 0:
                print(f"[warn] EcoTracker nicht lesbar ({errors}x): {exc}", flush=True)
        SHUTDOWN.wait(Config.POLL_INTERVAL)


def poll_inverter() -> None:
    if not Config.INVERTER_URL:
        return
    while not SHUTDOWN.is_set():
        try:
            data = fetch_json(Config.INVERTER_URL, Config.HTTP_TIMEOUT)
            value = float(dig(data, Config.INVERTER_FIELD))
            if Config.INVERTER_INVERT:
                value = -value
            # positiv = ECOVIS speist ins Haus, negativ = ECOVIS bezieht AC
            INVERTER.set(value)
        except Exception as exc:  # noqa: BLE001
            INVERTER.fail()
            _, _, _, errors = INVERTER.get()
            if errors in (1, 5) or errors % 30 == 0:
                print(f"[warn] Shelly nicht lesbar ({errors}x): {exc}", flush=True)
        SHUTDOWN.wait(Config.POLL_INTERVAL)


# --------------------------------------------------------------------------
# Kern
# --------------------------------------------------------------------------

class OutputEstimate:
    """Schaetzt die Ausgangsleistung des ECOVIS ohne Messung am Ausgang.

    Grundlage ist das gemessene Verhalten: Das Geraet verschiebt seine Leistung
    pro Regelzyklus um genau den gemeldeten Betrag (Faktor 1.0). Der Limiter
    weiss, was er gemeldet hat, und rechnet mit.

    Zusaetzliche Sicherung ohne jede Messung am Ausgang: Wird eingespeist,
    liefert der ECOVIS mindestens diesen Betrag. Die Einspeisung ist damit eine
    Untergrenze - und Unterschaetzung ist die gefaehrliche Richtung.
    """

    def __init__(self):
        self.value = 0.0
        self.last_apply = time.monotonic()

    def bleed(self, amount: float) -> None:
        """Schaetzung langsam abbauen, wenn sie offensichtlich verklemmt ist."""
        self.value = clamp(self.value - amount, 0.0, Config.DEVICE_MAX_W)

    def apply(self, value: float) -> None:
        """Ein Zyklus ist vorbei: das Geraet hat diesen Wert umgesetzt."""
        self.value = clamp(self.value + value, 0.0, Config.DEVICE_MAX_W)

    def correct(self, grid: float | None, measured: float | None) -> float:
        # Einspeisung als Untergrenze
        if grid is not None and grid < 0:
            self.value = max(self.value, -grid)
        # echte Messung, falls vorhanden, hat Vorrang
        if measured is not None:
            self.value = max(measured, 0.0)
        self.value = clamp(self.value, 0.0, Config.DEVICE_MAX_W)
        return self.value


class Limiter:
    def __init__(self):
        self.last_log = 0.0
        self.estimate = OutputEstimate()
        self.held = 0.0
        self.held_since = 0.0
        self.stuck_since = None
        self.smooth = None
        self.last_smooth = None

    def step(self) -> dict:
        grid, grid_age, grid_extra, _ = GRID.get()
        inv, inv_age, _, _ = INVERTER.get()
        now = time.monotonic()

        grid_ok = grid is not None and grid_age < Config.STALE_S
        inv_ok = inv is not None and inv_age < Config.STALE_S
        counters = {
            "energyCounterIn": grid_extra.get("energyCounterIn", 0.0),
            "energyCounterOut": grid_extra.get("energyCounterOut", 0.0),
            "agePower": grid_extra.get("agePower"),
        }

        headroom = None
        measured = inv if (Config.INVERTER_URL and inv_ok) else None
        output = self.estimate.value
        grid_raw = grid
        if grid_ok:
            grid = self.smoothed(grid, now)

        if not grid_ok:
            # Netzwert fehlt: herunterregeln. Die Schaetzung laeuft mit, sonst
            # steht sie nach dem Ausfall auf einem Wert, den es nicht mehr gibt.
            fresh = (Config.SAFE_REDUCE_HARD_W if grid_age > Config.STALE_HARD_S
                     else Config.SAFE_REDUCE_W)
            state = "failsafe_hard" if grid_age > Config.STALE_HARD_S else "failsafe"
        elif not Config.ENABLED:
            fresh, state = grid, "passthrough"
        elif Config.INVERTER_URL and not inv_ok:
            fresh, state = min(grid, 0.0), "hold_no_inverter"
        else:
            output = self.estimate.correct(grid, measured)
            headroom = Config.CAP_W - output
            allowed = headroom / max(1.0, Config.IN_FLIGHT)
            if grid > allowed:
                fresh, state = allowed, "capped"
            else:
                fresh, state = grid, "passthrough"

        if state.startswith(("passthrough", "capped")):
            gain = Config.REPORT_GAIN_UP if fresh > 0 else Config.REPORT_GAIN_DOWN
            fresh *= gain
            if abs(fresh) < Config.REPORT_DEADBAND_W:
                fresh = 0.0
        fresh = clamp(fresh, Config.MIN_REPORT_W, Config.MAX_REPORT_W)

        # --- Wert einen ganzen Zyklus festhalten --------------------------
        # Nur so wendet das Geraet genau den Wert an, den der Limiter mitzaehlt.
        due = (now - self.held_since) >= Config.APPLY_INTERVAL_S
        urgent = grid_ok and grid < -Config.FAST_EXPORT_W and fresh < self.held
        if due or urgent:
            self.estimate.apply(self.held)
            self.held = fresh
            self.held_since = now
        elif state in ("capped", "passthrough"):
            state += "_hold"
        reported = self.held

        # --- Verklemmung aufloesen ----------------------------------------
        if headroom is not None and headroom <= 1.0 and grid_ok and grid > 0:
            if self.stuck_since is None:
                self.stuck_since = now
            elif now - self.stuck_since >= Config.RESYNC_S:
                self.estimate.bleed(Config.RESYNC_W)
                self.stuck_since = now
                print("[info] Schaetzung steht am Deckel, Haus bezieht weiter - "
                      "baue {:.0f} W ab (jetzt {:.0f} W)".format(
                          Config.RESYNC_W, self.estimate.value), flush=True)
        else:
            self.stuck_since = None

        payload = self._payload(reported, state, counters, grid_raw, inv, headroom,
                                max(grid_age, inv_age if Config.INVERTER_URL else grid_age))
        payload["limiterOutputEstimate"] = round(self.estimate.value, 1)
        payload["limiterOutputSource"] = "shelly" if measured is not None else "schaetzung"
        payload["limiterGridSmoothed"] = None if not grid_ok else round(grid, 1)
        return payload

    def smoothed(self, value: float, now: float) -> float:
        """Traege Glaettung in Bezugsrichtung, sofortiger Durchgriff bei
        nennenswerter Einspeisung."""
        if Config.GRID_SMOOTH_S <= 0:
            return value
        if self.smooth is None or self.last_smooth is None:
            self.smooth, self.last_smooth = value, now
            return value
        if value < -Config.FAST_EXPORT_W:
            # Einspeisung: ungeglaettet durchreichen, Glaettung nachziehen
            self.smooth, self.last_smooth = value, now
            return value
        dt = max(0.0, now - self.last_smooth)
        alpha = 1.0 - math.exp(-dt / Config.GRID_SMOOTH_S) if dt > 0 else 0.0
        self.smooth += alpha * (value - self.smooth)
        self.last_smooth = now
        return self.smooth

    @staticmethod
    def _payload(reported: float, state: str, counters: dict,
                 grid, inv, headroom, age: float) -> dict:
        reported = round(float(reported), 1)
        third = round(reported / 3.0, 2)
        return {
            "power": reported,
            "powerPhase1": third, "powerPhase2": third, "powerPhase3": third,
            "energyCounterIn": counters["energyCounterIn"],
            "energyCounterOut": counters["energyCounterOut"],
            "limiterVersion": VERSION,
            "limiterState": state,
            "limiterGridPower": None if grid is None else round(grid, 1),
            "limiterInverterPower": None if inv is None else round(inv, 1),
            "limiterHeadroom": None if headroom is None else round(headroom, 1),
            "limiterReported": reported,
            "limiterCap": Config.CAP_W,
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

        print("Netz={} | ECOVIS={} | Spielraum={} | gemeldet={} | {}".format(
            fmt(payload["limiterGridPower"]),
            fmt(payload.get("limiterOutputEstimate")),
            fmt(payload["limiterHeadroom"]), fmt(payload["limiterReported"]),
            payload["limiterState"]), flush=True)


def control_loop() -> None:
    limiter = Limiter()
    next_tick = time.monotonic()
    while not SHUTDOWN.is_set():
        payload = limiter.step()
        STATE.publish(payload)
        limiter.maybe_log(payload)
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
        if path not in ("", "/v1/json", "/json"):
            return self.send_error(404)
        body = json.dumps(STATE.snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def main() -> int:
    def stop(*_args):
        print("[info] Beende Limiter", flush=True)
        SHUTDOWN.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print("[info] Limiter {} | Deckel {:.0f} W | Befehle in der Totzeit {:.1f} | "
          "Ausgangsleistung: {} | Begrenzung {}".format(
              VERSION, Config.CAP_W, Config.IN_FLIGHT,
              f"Shelly {Config.INVERTER_URL}" if Config.INVERTER_URL else "eigene Schaetzung",
              "aktiv" if Config.ENABLED else "AUS"), flush=True)

    server = ThreadingHTTPServer((Config.LISTEN_HOST, Config.LISTEN_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[info] Limiter laeuft auf http://{Config.LISTEN_HOST}:{Config.LISTEN_PORT}/v1/json",
          flush=True)

    threading.Thread(target=poll_grid, daemon=True).start()
    threading.Thread(target=poll_inverter, daemon=True).start()
    control_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
