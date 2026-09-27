#!/usr/bin/env python3
"""
Mitschnitt im Sekundentakt: Netzwert, gemeldeter Wert, Ausgangsleistung und
Leistungsfaktor nebeneinander - plus Auswertung, welche Groesse der anderen
vorauslaeuft.

Quellen:
  * EcoTracker   : Netzwert (direkt, unabhaengig von der Kette)
  * Limiter      : gemeldeter Wert und Zustand
  * Home Assistant: beliebige Entitaeten (Ausgangsleistung, Leistungsfaktor)

Home-Assistant-Token: Profil -> Sicherheit -> Langlebige Zugriffstoken.

Beispiel:
    export HA_TOKEN="eyJ..."
    export HA_ENTITIES="leistung=sensor.ecovis_ausgangsleistung,pf=sensor.ecovis_leistungsfaktor"
    python3 mitschnitt.py

Auswertung am Ende: Kreuzkorrelation zwischen der ersten und der zweiten
HA-Entitaet ueber verschiedene Zeitversaetze. Ein negativer bester Versatz
bedeutet, die zweite Groesse laeuft der ersten voraus.

Nur Standardbibliothek.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.request

STOP = False


def env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


ECOTRACKER_URL = env("ECOTRACKER_URL", "http://192.168.1.50:18080/v1/json")
LIMITER_URL = env("LIMITER_URL", "http://127.0.0.1:18081/v1/json")
HA_URL = env("HA_URL", "http://192.168.1.10:8123")
HA_TOKEN = env("HA_TOKEN")
HA_ENTITIES = env("HA_ENTITIES")          # "label=entity_id,label=entity_id"
DURATION_S = float(env("DURATION_S", "300"))
INTERVAL_S = float(env("INTERVAL_S", "1"))
CSV_PATH = env("CSV_PATH", "mitschnitt.csv")


def parse_entities(spec: str):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            label, entity = part.split("=", 1)
        else:
            label, entity = part.split(".")[-1][:12], part
        out.append((label.strip(), entity.strip()))
    return out


ENTITIES = parse_entities(HA_ENTITIES)


def fetch(url: str, token: str | None = None, timeout: float = 2.0):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_grid():
    try:
        return float(fetch(ECOTRACKER_URL)["power"])
    except Exception:  # noqa: BLE001
        return None


def read_limiter():
    try:
        data = fetch(LIMITER_URL)
        return float(data.get("power", 0.0)), data.get("limiterState", "-")
    except Exception:  # noqa: BLE001
        return None, "-"


def read_entity(entity_id: str):
    try:
        data = fetch(f"{HA_URL}/api/states/{entity_id}", HA_TOKEN)
        return float(data["state"])
    except Exception:  # noqa: BLE001
        return None


def pearson(xs, ys):
    pairs = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None]
    n = len(pairs)
    if n < 10:
        return None
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    sxx = sum((a - mx) ** 2 for a, _ in pairs)
    syy = sum((b - my) ** 2 for _, b in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def cross_correlate(first, second, max_lag: int):
    """Verschiebt die zweite Reihe gegen die erste und sucht die beste Lage."""
    results = []
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            a, b = first[-lag:], second[:len(second) + lag]
        elif lag > 0:
            a, b = first[:len(first) - lag], second[lag:]
        else:
            a, b = first, second
        r = pearson(a, b)
        if r is not None:
            results.append((lag, r))
    return results


def main() -> int:
    def stop(*_args):
        global STOP
        STOP = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if ENTITIES and not HA_TOKEN:
        print("[warn] HA_ENTITIES gesetzt, aber kein HA_TOKEN - "
              "die Spalten bleiben leer", flush=True)

    header = ["zeit", "netz_w", "gemeldet_w", "zustand"] + [l for l, _ in ENTITIES]
    print(" | ".join(f"{h:>12}" for h in header), flush=True)

    rows = []
    t0 = time.monotonic()
    next_tick = t0

    while not STOP and (time.monotonic() - t0) < DURATION_S:
        stamp = time.strftime("%H:%M:%S")
        grid = read_grid()
        reported, state = read_limiter()
        values = [read_entity(entity) for _, entity in ENTITIES]

        row = [stamp, grid, reported, state] + values
        rows.append(row)

        def fmt(value):
            if value is None:
                return "          --"
            if isinstance(value, str):
                return f"{value:>12}"
            return f"{value:12.2f}"

        print(" | ".join(fmt(v) for v in row), flush=True)

        next_tick += INTERVAL_S
        sleep_for = next_tick - time.monotonic()
        if sleep_for < 0:
            next_tick = time.monotonic()
            sleep_for = 0
        time.sleep(sleep_for)

    try:
        with open(CSV_PATH, "w", encoding="utf-8") as fh:
            fh.write(",".join(header) + "\n")
            for row in rows:
                fh.write(",".join("" if v is None else str(v) for v in row) + "\n")
        print(f"\n[info] {len(rows)} Zeilen geschrieben nach {CSV_PATH}", flush=True)
    except OSError as exc:
        print(f"[warn] CSV nicht schreibbar: {exc}", flush=True)

    # --- Auswertung -------------------------------------------------------
    grid_series = [r[1] for r in rows]
    reported_series = [r[2] for r in rows]

    print("\n=== Zusammenhaenge ===", flush=True)
    max_lag = int(min(30, len(rows) // 4))

    def report(name_a, series_a, name_b, series_b):
        results = cross_correlate(series_a, series_b, max_lag)
        if not results:
            print(f"{name_a} vs {name_b}: zu wenig Daten", flush=True)
            return
        best = max(abs(r) for _, r in results)
        # Bei periodischen Signalen (Grenzzyklus!) ist eine Verschiebung um
        # eine halbe Periode gleich stark korreliert, nur mit umgekehrtem
        # Vorzeichen. Deshalb unter gleichwertigen Lagen die kleinste nehmen.
        candidates = [(lag, r) for lag, r in results if abs(r) >= best - 0.02]
        lag, r = min(candidates, key=lambda item: abs(item[0]))
        if lag < 0:
            direction = f"{name_b} laeuft {abs(lag)} s voraus"
        elif lag > 0:
            direction = f"{name_b} laeuft {lag} s hinterher"
        else:
            direction = "gleichzeitig"
        print(f"{name_a} vs {name_b}: r={r:+.2f} bei {lag:+d} s  ->  {direction}",
              flush=True)

    report("netz", grid_series, "gemeldet", reported_series)

    for index, (label, _) in enumerate(ENTITIES):
        series = [r[4 + index] for r in rows]
        report("netz", grid_series, label, series)

    if len(ENTITIES) >= 2:
        first = [r[4] for r in rows]
        second = [r[5] for r in rows]
        report(ENTITIES[0][0], first, ENTITIES[1][0], second)
        print("\nLaeuft der Leistungsfaktor der Leistung hinterher, ist er Folge.\n"
              "Laeuft er voraus, passiert im Geraet etwas anderes.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
