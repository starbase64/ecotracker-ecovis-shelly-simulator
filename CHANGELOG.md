# Changelog

## 4.0.0
- Das Projekt ist jetzt ausdrücklich ein gedämpfter EcoTracker–Shelly-Proxy
  und kein Leistungsbegrenzer.
- Experimentellen Deckel, Ausgangsschätzung, Shelly-Ausgangsmessung und alle
  zugehörigen Optionen entfernt. Der Ansatz kann prinzipbedingt keine sichere
  800-W-Grenze bereitstellen.
- Hauptprogramm und Compose-Dienst heißen jetzt `ecotracker_shelly_proxy.py`
  beziehungsweise `ecotracker-proxy`. Der alte Python-Dateiname bleibt als
  Kompatibilitätsstarter erhalten.
- Eindeutige Diagnosefelder für Rohwert, geglätteten Wert, Verstärkung,
  Richtung, Datenalter und Verfügbarkeit von `agePower` ergänzt.
- Fehlendes `agePower` wird akzeptiert, aber sichtbar markiert und einmalig
  protokolliert.
- Failsafe und deutliche Einspeisung umgehen das Haltefenster sofort.
- Docker-Healthcheck und automatisierte Tests ergänzt.
- Dokumentation trennt Nulleinspeisungsregelung und Leistungsbegrenzung klar.

## 3.5.0
- **Daempfung statt Glaettung.** `REPORT_GAIN_UP` (0.4) und `REPORT_GAIN_DOWN`
  (0.6): es wird nur ein Bruchteil des Netzwerts gemeldet. Der gemessene
  Grenzzyklus (Periode ~60 s, Amplitude ±160 W) verschwindet damit.
- `GRID_SMOOTH_S` von 15 auf 3 s gesenkt. Eine lange Glaettung fuegt
  Phasenverzug hinzu und facht den Grenzzyklus an, statt ihn zu daempfen.

## 3.4.0
- Einseitige Glaettung des Netzwerts (`GRID_SMOOTH_S`), Einspeisung ueber
  `FAST_EXPORT_W` geht ungeglaettet durch.
- `REPORT_DEADBAND_W`: kleine Abweichungen werden als 0 gemeldet, das Geraet
  haelt statt um den Nullpunkt zu zucken.
- Diagnosefeld `limiterGridSmoothed`.

## 3.3.0
- **Fehlerbehebung:** Die Schaetzung der Ausgangsleistung lief im Ausfallzweig
  nicht mit. Nach einem EcoTracker-Ausfall stand sie am Deckel, waehrend das
  Geraet bereits auf 0 gefahren war — der Limiter meldete danach dauerhaft 0.
- Ein fehlendes `agePower`-Feld loest keinen Ausfall mehr aus. Der EcoTracker
  liefert es zeitweise nicht mit; der Messwert selbst ist vorhanden.
- `RESYNC_S` / `RESYNC_W`: verklemmte Schaetzung wird langsam abgebaut.

## 3.2.0
- Der gemeldete Wert wird einen vollen Regelzyklus festgehalten, damit das
  Geraet genau den Wert anwendet, den der Limiter mitzaehlt.

## 3.1.0
- Schaetzung der Ausgangsleistung ohne Shelly, mit der Einspeisung als
  Untergrenze. `INVERTER_URL` wurde optional.

## 3.0.0
- Neuer Ansatz: kein berechneter Korrekturwert mehr, sondern Durchleiten des
  echten Netzwerts mit Begrenzung auf den Spielraum bis zum Deckel.
- Entfallen: KP, Totband, Notzweig.

## 2.2.0
- Sonderzweig fuer kleine Lasten, Verarbeitung von AC-Bezug des Geraets.

## 2.1.x
- Totzeitnormierter Regler mit KP, Totband und Notzweig. Setzte voraus, dass das
  Geraet die Korrektur einmal pro Sekunde anwendet — tatsaechlich fragt es nur
  alle 2,3 bis 3,5 s ab und handelt alle 5 s.
