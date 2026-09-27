# EcoTracker → Shelly Pro 3EM Simulator für ECO-WORTHY ECOVIS 2400

Der ECOVIS 2400 (ECO-BPS2400WDZ) akzeptiert als Stromzähler einen Shelly 3EM
oder Shelly Pro 3EM. Dieses Projekt simuliert einen Shelly Pro 3EM und speist
ihn mit den lokalen Messwerten eines everHome EcoTrackers.

```text
EcoTracker ──HTTP/JSON──▶ Dämpfungs-Proxy :18081 ──▶ uni-meter
                                                        │
                                             Shelly-Pro-3EM-Protokoll
                                                        │
                                                        ▼
                                                   ECOVIS 2400
```

Getesteter Stand: Proxy 4.0.0, sdeigm/uni-meter 1.5.0 und ECO-WORTHY
ECOVIS 2400.

> **Keine 800-W-Begrenzung:** Der ECOVIS versteht den Zählerwert als
> wiederholte Korrektur und nicht als absoluten Leistungssollwert. Dieses
> Projekt stabilisiert die Nulleinspeisungsregelung, begrenzt aber nicht die
> Ausgangsleistung. Im Automatikmodus kann das Gerät bis 1.600 W hochregeln.

## Funktionsweise

Ein echter Stromzähler meldet, was am Hausanschluss passiert. Meldet er 100 W
Netzbezug, erhöht der ECOVIS seine Ausgangsleistung um ungefähr 100 W. Beim
nächsten Regelzyklus verarbeitet er den dann vorhandenen Netzbezug erneut.

Wegen der gemessenen Totzeit sind zwei bis drei Korrekturen gleichzeitig
unterwegs. Der volle Netzwert führt deshalb zu einem Grenzzyklus. Der Proxy
meldet nur einen Anteil:

```text
Bezug:       gemeldeter Wert = Netzwert × 0,4
Einspeisung: gemeldeter Wert = Netzwert × 0,6
```

Der ECOVIS bleibt der einzige Regler. Der Proxy berechnet weder eine eigene
Ausgangsleistung noch einen Leistungssollwert.

## Gemessenes Geräteverhalten

Alle Werte stammen aus Messungen am eigenen Gerät.

| Eigenschaft | Messwert | Methode |
|---|---:|---|
| Zählerabfrage | UDP-Broadcast Port 8888 alle 2,3–3,5 s | `tcpdump` |
| Regelzyklus | ungefähr 5 s | Sprungantwort |
| Verstärkung | etwa 1,0 × gemeldeter Wert je Zyklus | Sprungantwort |
| Totzeit bis sichtbare Wirkung | ungefähr 9,5 s | Sprungantwort |
| Verhalten bei Datenausfall | letzter Zustand wird unbegrenzt gehalten | Ausfalltest |
| Wiederanlauf | automatisch ohne erneute Kopplung | Ausfalltest |
| Leistungsfaktor | 0,94 bei 130 W bis 0,99 bei 300 W | Mitschnitt |
| Wirkungsgrad AC/Batterie | 74 % bei 130 W bis 88 % bei 300 W | Mitschnitt |

Mit den Standardwerten `REPORT_GAIN_UP=0.4` und
`REPORT_GAIN_DOWN=0.6` verschwand der zuvor gemessene Grenzzyklus von ungefähr
±160 W bei rund 60 Sekunden Periodendauer. Im gemessenen Dauerbetrieb lag der
Netzwert zwischen −15 und +11 W. Ein Lastsprung von 900 W war nach ungefähr
25 Sekunden eingeschwungen.

## Warum der Zählersimulator keine 800-W-Grenze setzen kann

Der Zählerwert ist kein Sollwert, sondern eine Korrektur. Eine Begrenzung des
gemeldeten Werts auf beispielsweise 790 W begrenzt nur einen einzelnen Schritt.
Bei 2.000 W Hauslast kann der ECOVIS denselben Schritt wiederholt addieren und
weiter bis zur Geräteobergrenze steigen.

Auch eine mitlaufende Schätzung ist nicht zuverlässig: Setzt das Gerät einen
Befehl nicht um, weichen Schätzung und Realität dauerhaft voneinander ab. Die
verfügbare Cloud-Telemetrie aktualisierte sich im Test nur etwa alle 37 Sekunden
und ist bei 9,5 Sekunden Totzeit zu langsam.

Eine echte Grenze benötigt einen absoluten Leistungssollwert. Sie gehört daher
in den manuellen Betriebsmodus beziehungsweise in eine direkte Gerätesteuerung,
nicht in das Shelly-Zählerprotokoll.

## Installation

```bash
git clone https://github.com/starbase64/ecotracker-ecovis-shelly-simulator.git
cd ecotracker-ecovis-shelly-simulator
cp .env.example .env
nano .env
docker compose up -d
docker compose logs -f ecotracker-proxy
```

In `.env` muss `ECOTRACKER_URL` auf die lokale EcoTracker-Schnittstelle zeigen.
Die mitgelieferte `uni-meter.conf` verwendet den Proxy unter
`http://127.0.0.1:18081/v1/json` und UDP-Port 8888. Beide Container laufen im
Host-Netz, weil der ECOVIS den simulierten Shelly per UDP-Broadcast sucht.

### Aktualisierung von Version 3.5

Version 4.0 benennt Dienst, Container und Hauptprogramm um und entfernt die
gescheiterte experimentelle Leistungsbegrenzung vollständig:

```bash
cd /home/maik/shelly_simulator
cp -a docker-compose.yml docker-compose.yml.bak-3.5
cp -a ecotracker_limiter.py ecotracker_limiter.py.bak-3.5

# Neue Dateien aus dem Paket in dieses Verzeichnis kopieren, dann:
docker compose down
docker compose up -d
docker compose logs --since=2m ecotracker-proxy ecotracker-shelly
./check.sh
```

Der alte Dateiname `ecotracker_limiter.py` bleibt als Kompatibilitätsstarter
erhalten, wird von der neuen Compose-Datei aber nicht mehr verwendet.

## Konfiguration

| Variable | Vorgabe | Bedeutung |
|---|---:|---|
| `ECOTRACKER_URL` | `http://192.168.1.50:18080/v1/json` | lokale EcoTracker-API |
| `ECOTRACKER_MAX_AGE_MS` | `3000` | maximales `agePower`, sofern vorhanden |
| `POLL_INTERVAL` | `1.0` | Abfragetakt in Sekunden |
| `LISTEN_HOST` / `LISTEN_PORT` | `127.0.0.1` / `18081` | lokaler Proxy-Endpunkt |
| `REPORT_GAIN_UP` | `0.4` | Anteil bei Netzbezug |
| `REPORT_GAIN_DOWN` | `0.6` | Anteil bei Einspeisung |
| `REPORT_DEADBAND_W` | `10` | kleinere geglättete Abweichungen melden 0 W |
| `REPORT_HOLD_S` | `5` | Korrekturwert für einen Gerätezyklus halten |
| `GRID_SMOOTH_S` | `3` | kurze Glättung gegen Messrauschen |
| `FAST_EXPORT_W` | `30` | stärkere Einspeisung sofort nachführen |
| `MAX_CORRECTION_UP_W` | `800` | maximal gemeldeter positiver Einzelschritt; **kein Ausgangslimit** |
| `MAX_CORRECTION_DOWN_W` | `2000` | maximaler Absenkschritt als positiver Betrag |
| `STALE_S` | `5` | danach gilt die Quelle als veraltet |
| `STALE_HARD_S` | `15` | danach wird der harte Failsafe gemeldet |
| `SAFE_REDUCE_W` | `-300` | Absenksignal bei kurzem Datenausfall |
| `SAFE_REDUCE_HARD_W` | `-800` | Absenksignal bei längerem Datenausfall |

Die Standardverstärkungen beruhen auf Messungen am Testgerät. Änderungen
sollten mit einem Mitschnitt überprüft werden. Die Grenzen
`MAX_CORRECTION_*` begrenzen ausschließlich einen gemeldeten Korrekturschritt.

## Diagnose

```bash
./check.sh
```

Oder einzeln:

```bash
curl -s http://127.0.0.1:18081/v1/json | python3 -m json.tool
curl -s http://127.0.0.1:18081/healthz | python3 -m json.tool
curl -s 'http://127.0.0.1/rpc/EM.GetStatus?id=0' | python3 -m json.tool
```

| Feld | Bedeutung |
|---|---|
| `proxyState` | `damped_raise`, `damped_reduce`, `deadband`, Failsafe oder `_hold` |
| `gridPowerRaw` | unveränderter EcoTracker-Netzwert |
| `gridPowerSmoothed` | Netzwert nach kurzer Glättung |
| `reportedPower` | an uni-meter ausgegebener Korrekturwert |
| `reportGain` | aktuell verwendeter Faktor |
| `reportDirection` | erhöhen, absenken oder halten |
| `dataHealthy` | EcoTracker-Wert ist lokal frisch |
| `dataAgeSeconds` | Alter der letzten erfolgreichen HTTP-Abfrage |
| `ecoTrackerAgePowerMs` | vom EcoTracker gemeldetes Alter oder `null` |
| `ecoTrackerAgePowerAvailable` | zeigt, ob `agePower` geliefert wurde |

Die alten Felder `limiterVersion`, `limiterState`, `limiterGridPower`,
`limiterGridSmoothed`, `limiterReported` und `limiterEcoTrackerAgeMs` bleiben
vorerst als Kompatibilitätsaliase erhalten.

## Ausfallverhalten

| Ausfall | Reaktion |
|---|---|
| EcoTracker nicht erreichbar | sofort negatives Korrektursignal; nach `STALE_HARD_S` stärkeres Signal |
| Proxy gestoppt | uni-meter vergisst den letzten Wert nach seinem `forget-interval`; ECOVIS hält anschließend seinen Zustand |
| uni-meter gestoppt | ECOVIS hält seinen Zustand und koppelt nach dem Neustart automatisch wieder |

Der Proxy kann seinen eigenen Ausfall nicht absichern. Wer eine unabhängige
Schutzebene benötigt, muss den AC-Ausgang außerhalb dieser Software überwachen
und gegebenenfalls abschalten.

## Messwerkzeuge

`tools/ecovis_steptest.py` erzeugt definierte Sprünge und protokolliert die
Reaktion. Das Werkzeug ersetzt den Proxy vorübergehend und besitzt Grenzwerte
für Ausgangsleistung und Einspeisung.

`tools/mitschnitt.py` zeichnet EcoTracker, Proxy und optionale
Home-Assistant-Entitäten gemeinsam auf. Es berechnet Kreuzkorrelationen sowie
Mittelwert, mittlere Nullabweichung, Spitzenwerte und den Anteil innerhalb
von ±15 W.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile ecotracker_shelly_proxy.py ecotracker_limiter.py tools/*.py
```

## Bekannte Grenzen

- Keine Begrenzung der ECOVIS-Ausgangsleistung; im Automatikmodus sind bis zu
  1.600 W möglich.
- Schnell taktende Lasten können wegen der gemessenen Totzeit nicht vollständig
  ausgeregelt werden.
- Kein Schutz gegen den Ausfall des Proxy- oder uni-meter-Containers selbst.
- Inoffizielles Projekt ohne Verbindung zu ECO-WORTHY oder everHome.

## Lizenz

MIT
