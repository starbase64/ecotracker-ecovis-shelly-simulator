# EcoTracker → Shelly Pro 3EM Simulator für ECO-WORTHY ECOVIS 2400

Der ECOVIS 2400 (ECO-BPS2400WDZ) akzeptiert als Stromzähler nur einen Shelly 3EM
oder Shelly Pro 3EM. Dieses Projekt stellt ihm einen solchen Zähler bereit,
gespeist aus den Messwerten eines everHome EcoTracker.

```
EcoTracker  ──HTTP/JSON──▶  Limiter :18081  ──HTTP/JSON──▶  uni-meter
                                                                 │
                                                        Shelly Pro 3EM
                                                       (HTTP 80, UDP 8888)
                                                                 │
                                                                 ▼
                                                          ECOVIS 2400
```

Der Limiter sitzt zwischen EcoTracker und uni-meter. Er reicht den Netzwert
durch, dämpft ihn und sorgt für definiertes Verhalten bei Datenausfall.

Getesteter Stand: Limiter 3.5.0, sdeigm/uni-meter:1.5.0, ECO-WORTHY ECOVIS 2400.

---

## Gemessenes Verhalten des ECOVIS

Alle Werte stammen aus Messungen am eigenen Gerät, nicht aus Dokumentation.

| Eigenschaft | Messwert | Methode |
|---|---|---|
| Abfrage des Zählers | UDP-Broadcast auf Port 8888, alle 2,3–3,5 s | tcpdump |
| Regelzyklus | ~5 s | Sprungantwort |
| Verstärkung | 1,0 × gemeldeter Wert pro Zyklus | Sprungantwort |
| Totzeit bis sichtbare Wirkung | ~9,5 s | Sprungantwort |
| Verhalten bei Datenausfall | hält den letzten Zustand unbegrenzt | Ausfalltest |
| Wiederanlauf | automatisch, ohne Neukopplung | Ausfalltest |
| Leistungsfaktor | reine Funktion der Leistung (0,94 bei 130 W … 0,99 bei 300 W) | Mitschnitt |
| Wirkungsgrad AC/Batterie | 74 % bei 130 W … 88 % bei 300 W | Mitschnitt |

**Wichtig:** Der ECOVIS behandelt den gemeldeten Netzwert als *Korrektur*, nicht
als Sollwert. Er verschiebt seine Ausgangsleistung pro Zyklus um genau diesen
Betrag. Ein echter Shelly meldet den tatsächlichen Netzwert, wodurch sich die
Abweichung nach jeder Korrektur selbst verkleinert — die Schleife trägt sich ab.

---

## Warum es keine Leistungsbegrenzung über den Zähler gibt

Mehrere Versuche (Limiter 2.0 bis 3.3) haben versucht, über den Zählerwert eine
Obergrenze von 800 W zu erzwingen. Das funktioniert nicht, und zwar prinzipiell:

* **Der Zählerwert ist kein Sollwert.** Ein Deckel bei 790 W begrenzt die
  Schrittweite, nicht das Ergebnis. Das Gerät addiert jeden Zyklus erneut und
  klettert trotzdem über die Grenze.
* **Ohne Messung am Ausgang lässt sich die Ausgangsleistung nicht bestimmen.**
  Eine mitlaufende Schätzung driftet, sobald das Gerät einen Befehl nicht
  umsetzt — beobachtet und dokumentiert.
* **Die Telemetrie der Cloud-Bridge ist zu langsam.** Die AC-Leistung
  aktualisiert nur etwa alle 37 s, bei 9,5 s Totzeit unbrauchbar.

Die Leistungsbegrenzung gehört deshalb dorthin, wo es einen echten Sollwert
gibt: in den manuellen Betriebsmodus des Geräts, gesetzt über die
[ecovis-home-assistant-bridge](https://github.com/starbase64/ecovis-home-assistant-bridge).
Der Zählersimulator bleibt für die Nullregelung zuständig.

---

## Dämpfung statt Glättung

Wird der volle Netzwert gemeldet, korrigiert der ECOVIS jede Abweichung
mehrfach: In der Totzeit von 9,5 s bei 5 s Regelzyklus sind stets zwei bis drei
Befehle unterwegs. Das Ergebnis ist ein Grenzzyklus — gemessen mit einer Periode
von rund 60 s und einer Amplitude von ±160 W.

Eine EMA-Glättung verschlimmert das, weil sie Phasenverzug hinzufügt. Die
wirksame Schraube ist die Verstärkung: Wird nur ein Bruchteil des Netzwerts
gemeldet, klingt die Abweichung geometrisch ab.

Mit `REPORT_GAIN_UP=0.4` und `REPORT_GAIN_DOWN=0.6` verschwindet der
Grenzzyklus. Gemessen im Dauerbetrieb: Netzwert zwischen −15 und +11 W,
Einschwingen nach einem Lastsprung von 900 W in etwa 25 s.

---

## Installation

```bash
git clone https://github.com/starbase64/ecotracker-ecovis-shelly-simulator.git
cd ecotracker-ecovis-shelly-simulator
cp .env.example .env
# In .env die Adresse des eigenen EcoTrackers eintragen
# Die mitgelieferte uni-meter.conf bei Bedarf an das eigene Netz anpassen
docker compose up -d
docker compose logs -f ecotracker-limiter
```

Die erste Logzeile nennt Version und Betriebsart:

```
[info] Limiter 3.5.0 | Deckel 760 W | Befehle in der Totzeit 3.0 | Ausgangsleistung: eigene Schaetzung | Begrenzung AUS
```

### uni-meter.conf

Die Konfiguration von uni-meter gehört ins selbe Verzeichnis. Zwei Punkte sind
entscheidend:

* Eingang `generic-http` auf `http://127.0.0.1:18081/v1/json`, Feld `$.power`
* Ausgang `shelly-pro3em` auf Port 80 und `udp-port = 8888`

Der ECOVIS sucht per UDP-Broadcast, deshalb brauchen beide Container
`network_mode: host`.

---

## Konfiguration

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `ECOTRACKER_URL` | `http://192.168.1.50:18080/v1/json` | Beispieladresse; in `.env` an das eigene Netz anpassen |
| `ECOTRACKER_MAX_AGE_MS` | `3000` | Grenze für `agePower`; fehlt das Feld, wird der Wert akzeptiert |
| `POLL_INTERVAL` | `1.0` | Abfragetakt des Limiters |
| `LISTEN_HOST` / `LISTEN_PORT` | `127.0.0.1` / `18081` | eigener Endpunkt für uni-meter |
| **Dämpfung** | | |
| `REPORT_GAIN_UP` | `0.4` | Anteil des gemeldeten Werts bei Bezug |
| `REPORT_GAIN_DOWN` | `0.6` | Anteil bei Einspeisung |
| `REPORT_DEADBAND_W` | `10` | darunter wird 0 gemeldet, das Gerät hält |
| `GRID_SMOOTH_S` | `3.0` | leichte Glättung gegen Messrauschen; Einspeisung über `FAST_EXPORT_W` geht ungeglättet durch |
| `FAST_EXPORT_W` | `30` | ab dieser Einspeisung sofort nachführen |
| **Ausfall** | | |
| `STALE_S` | `5` | danach gilt der Netzwert als veraltet |
| `STALE_HARD_S` | `15` | danach maximale Absenkung |
| `SAFE_REDUCE_W` | `-300` | gemeldeter Wert im Ausfall |
| `SAFE_REDUCE_HARD_W` | `-800` | gemeldeter Wert im harten Ausfall |
| **Deckel (experimentell, siehe oben)** | | |
| `ENABLED` | `true` | `false` = reiner Durchleitbetrieb, empfohlen |
| `CAP_W` | `760` | Obergrenze, nur mit `INVERTER_URL` sinnvoll |
| `IN_FLIGHT` | `3.0` | angenommene Zahl gleichzeitig unterwegs befindlicher Befehle |
| `INVERTER_URL` | leer | Messung am Ausgang, z. B. ein Shelly; leer = Schätzung |
| `APPLY_INTERVAL_S` | `5.0` | Regelzyklus des Geräts |
| `RESYNC_S` / `RESYNC_W` | `60` / `25` | Abbau einer verklemmten Schätzung |

### Empfohlene Einstellung

```yaml
ENABLED: "false"
REPORT_GAIN_UP: "0.4"
REPORT_GAIN_DOWN: "0.6"
REPORT_DEADBAND_W: "10"
GRID_SMOOTH_S: "3"
FAST_EXPORT_W: "30"
```

Schwingt es, `REPORT_GAIN_UP` senken (0,25). Ist es zu träge, vorsichtig anheben
(0,5). `REPORT_GAIN_DOWN` nicht unter 0,5, sonst dauern Einspeisephasen nach
einem Lastabwurf länger.

---

## Diagnose

```bash
curl -s http://127.0.0.1:18081/v1/json | python3 -m json.tool
```

| Feld | Bedeutung |
|---|---|
| `limiterState` | `passthrough`, `capped`, `failsafe`, `failsafe_hard`, Zusatz `_hold` während eines Haltefensters |
| `limiterGridPower` | Netzwert roh |
| `limiterGridSmoothed` | nach Glättung |
| `limiterReported` | was uni-meter ausliefert |
| `limiterOutputEstimate` | geschätzte Ausgangsleistung (nur mit Deckel) |
| `limiterEcoTrackerAgeMs` | Alter des Messwerts laut EcoTracker |

---

## Werkzeuge

**`tools/ecovis_steptest.py`** — Sprungantwort messen. Ersetzt den Limiter
vorübergehend, meldet einen festen Wert und protokolliert die Reaktion. Damit
wurden Verstärkung, Regelzyklus und Totzeit bestimmt. Abbruchschutz bei zu hoher
Ausgangsleistung oder Einspeisung.

```bash
docker compose stop ecotracker-limiter
python3 tools/ecovis_steptest.py
docker compose start ecotracker-limiter
```

**`tools/mitschnitt.py`** — Netzwert, gemeldeten Wert und beliebige
Home-Assistant-Entitäten im Sekundentakt nebeneinander protokollieren, mit
Kreuzkorrelation am Ende. Damit wurde gezeigt, dass der Leistungsfaktor der
Leistung folgt und nicht umgekehrt.

```bash
export HA_TOKEN="$(cat ~/.ha_token)"
export HA_ENTITIES="leistung=sensor.ecovis_2400_ac_leistung,pf=sensor.ecovis_2400_leistungsfaktor"
DURATION_S=300 python3 tools/mitschnitt.py
```

---

## Ausfallverhalten

| Ausfall | Reaktion |
|---|---|
| EcoTracker nicht erreichbar | Limiter meldet `SAFE_REDUCE_W`, nach `STALE_HARD_S` den harten Wert — der ECOVIS fährt herunter |
| Limiter gestoppt | uni-meter liefert den letzten Wert für die Dauer seines `forget-interval`, danach eine leere Antwort. Der ECOVIS **hält** seinen Zustand unbegrenzt |
| uni-meter gestoppt | Der ECOVIS fragt weiter, bekommt nichts, hält. Nach dem Start koppelt er automatisch wieder |

Der zweite und dritte Fall sind nicht durch den Limiter abgedeckt — er läuft
dann ja selbst nicht. Wer einen harten Schutz braucht, sollte einen Schalter am
AC-Ausgang von außerhalb dieser Kette überwachen, etwa per Automation in Home
Assistant auf Basis der EcoTracker-Werte.

---

## Nicht gelöst

* Keine Leistungsbegrenzung über den Zähler (siehe oben). Im Automatikmodus geht
  das Gerät bis 1.600 W.
* Lasten, die schneller schalten als die Totzeit (Herdplatte mit
  Zweipunktregelung), lassen sich nicht ausregeln. Gemessene Ausschläge bis
  −485 W bei einem 900-W-Takt.
* Kein Schutz gegen den Ausfall des Limiters selbst.

---

## Lizenz

MIT. Inoffiziell, ohne Gewähr, kein Bezug zu ECO-WORTHY oder everHome.
