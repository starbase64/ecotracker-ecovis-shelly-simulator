#!/usr/bin/env python3
"""
ECOVIS Sprungantwort-Messung
============================

Ersetzt den Limiter voruebergehend und meldet uni-meter einen FESTEN Wert.
Protokolliert dabei, wie schnell der ECOVIS seine Ausgangsleistung verschiebt.
Ergebnis ist die Streckenverstaerkung: Watt Leistungsaenderung pro gemeldetem
Watt und pro Sekunde. Daraus laesst sich KP sauber berechnen.

Ablauf:
  Phase 1  BASELINE_S   Wert 0 melden, Ruhelage aufnehmen
  Phase 2  STEP_S       Wert STEP_W melden, Reaktion messen
  Phase 3               Wert STOP_W melden, bis die Leistung wieder faellt

Sicherheitsabbruch: ueberschreitet die gemessene Ausgangsleistung ABORT_W oder
die Einspeisung ABORT_EXPORT_W, schaltet das Werkzeug sofort auf Phase 3.

Vorher den Limiter stoppen, sonst ist Port 18081 belegt:
    docker stop ecotracker-limiter

Start (Beispiel):
    python3 ecovis_steptest.py

Danach:
    docker start ecotracker-limiter

Nur Standardbibliothek.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "ja")


# --- Konfiguration --------------------------------------------------------
ECOTRACKER_URL = env_str("ECOTRACKER_URL", "http://192.168.1.50:18080/v1/json")
INVERTER_URL = env_str("INVERTER_URL", "http://192.168.1.51/rpc/Switch.GetStatus?id=0")
INVERTER_FIELD = env_str("INVERTER_FIELD", "apower")
INVERTER_INVERT = env_bool("INVERTER_INVERT", True)

LISTEN_HOST = env_str("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(env_float("LISTEN_PORT", 18081))

BASELINE_S = env_float("BASELINE_S", 20.0)
STEP_S = env_float("STEP_S", 60.0)
STEP_W = env_float("STEP_W", 100.0)      # gemeldeter Bezug -> ECOVIS soll hochfahren
STOP_W = env_float("STOP_W", -400.0)     # gemeldete Einspeisung -> wieder runter
RECOVER_S = env_float("RECOVER_S", 90.0)

ABORT_W = env_float("ABORT_W", 700.0)            # Ausgangsleistung
ABORT_EXPORT_W = env_float("ABORT_EXPORT_W", 400.0)  # Einspeisung am Hausanschluss
SAMPLE_S = env_float("SAMPLE_S", 1.0)
CSV_PATH = env_str("CSV_PATH", "steptest.csv")

reported = 0.0
lock = threading.Lock()
SHUTDOWN = threading.Event()


def fetch_json(url: str, timeout: float = 1.5):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def dig(data, path: str):
    cur = data
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


def read_inverter():
    try:
        value = float(dig(fetch_json(INVERTER_URL), INVERTER_FIELD))
        return -value if INVERTER_INVERT else value
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Shelly nicht lesbar: {exc}", flush=True)
        return None


def read_grid():
    try:
        return float(fetch_json(ECOTRACKER_URL)["power"])
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] EcoTracker nicht lesbar: {exc}", flush=True)
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("", "/v1/json", "/json"):
            return self.send_error(404)
        with lock:
            value = round(reported, 1)
        third = round(value / 3.0, 2)
        body = json.dumps({
            "power": value, "powerPhase1": third, "powerPhase2": third,
            "powerPhase3": third, "energyCounterIn": 0.0, "energyCounterOut": 0.0,
            "steptest": True,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def set_reported(value: float):
    global reported
    with lock:
        reported = value


def main() -> int:
    signal.signal(signal.SIGINT, lambda *_: SHUTDOWN.set())
    signal.signal(signal.SIGTERM, lambda *_: SHUTDOWN.set())

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[info] Messquelle laeuft auf http://{LISTEN_HOST}:{LISTEN_PORT}/v1/json", flush=True)
    print(f"[info] Phasen: {BASELINE_S:.0f}s Null, {STEP_S:.0f}s bei {STEP_W:+.0f} W, "
          f"danach {STOP_W:+.0f} W bis Ruhe", flush=True)
    print("[info] Abbruch bei Ausgang > {:.0f} W oder Einspeisung > {:.0f} W".format(
        ABORT_W, ABORT_EXPORT_W), flush=True)

    rows = []
    t0 = time.monotonic()
    phase = "baseline"
    set_reported(0.0)
    phase_start = t0
    step_begin_power = None
    aborted = False

    while not SHUTDOWN.is_set():
        now = time.monotonic()
        elapsed = now - t0
        inv = read_inverter()
        grid = read_grid()

        if inv is not None and inv > ABORT_W and phase == "step":
            print(f"[ABBRUCH] Ausgangsleistung {inv:.0f} W ueber Grenze", flush=True)
            aborted = True
        if grid is not None and grid < -ABORT_EXPORT_W and phase == "step":
            print(f"[ABBRUCH] Einspeisung {abs(grid):.0f} W ueber Grenze", flush=True)
            aborted = True

        with lock:
            value = reported
        rows.append((round(elapsed, 1), phase, value,
                     None if inv is None else round(inv, 1),
                     None if grid is None else round(grid, 1)))
        print("t={:6.1f}s | {:9s} | gemeldet={:7.1f} W | ECOVIS={:>7} | Netz={:>7}".format(
            elapsed, phase, value,
            "  --  " if inv is None else f"{inv:7.1f}",
            "  --  " if grid is None else f"{grid:7.1f}"), flush=True)

        if phase == "baseline" and now - phase_start >= BASELINE_S:
            step_begin_power = inv if inv is not None else 0.0
            phase, phase_start = "step", now
            set_reported(STEP_W)
            print(f"[info] --- Sprung auf {STEP_W:+.0f} W, Startleistung {step_begin_power:.0f} W ---",
                  flush=True)
        elif phase == "step" and (aborted or now - phase_start >= STEP_S):
            step_end_power = inv
            step_seconds = now - phase_start
            phase, phase_start = "recover", now
            set_reported(STOP_W)
            print("[info] --- Sprung beendet ---", flush=True)
            if step_begin_power is not None and step_end_power is not None:
                delta = step_end_power - step_begin_power
                per_s = delta / max(1.0, step_seconds)
                print("\n=== Ergebnis ===", flush=True)
                print(f"gemeldeter Wert     : {STEP_W:+.0f} W", flush=True)
                print(f"Leistungsaenderung  : {delta:+.0f} W in {step_seconds:.0f} s", flush=True)
                print(f"Anstieg             : {per_s:+.1f} W/s", flush=True)
                print(f"Streckenverstaerkung: {per_s / STEP_W:.3f} (W/s je gemeldetem W)",
                      flush=True)
                print("KP-Empfehlung: control = error * SICHERHEIT / (G * SETTLE_S),", flush=True)
                print("   G = obiger Wert, SICHERHEIT etwa 0.5, SETTLE_S = Totzeit.\n", flush=True)
        elif phase == "recover":
            if (inv is not None and inv < 50.0) or now - phase_start >= RECOVER_S:
                break

        SHUTDOWN.wait(SAMPLE_S)

    set_reported(0.0)
    time.sleep(2)

    try:
        with open(CSV_PATH, "w", encoding="utf-8") as fh:
            fh.write("sekunden,phase,gemeldet_w,ecovis_w,netz_w\n")
            for row in rows:
                fh.write(",".join("" if v is None else str(v) for v in row) + "\n")
        print(f"[info] Messwerte geschrieben nach {CSV_PATH}", flush=True)
    except OSError as exc:
        print(f"[warn] CSV nicht schreibbar: {exc}", flush=True)

    print("[info] Fertig. Jetzt: docker start ecotracker-limiter", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
