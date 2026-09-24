# Changelog

## 2.1.1 - 2026-09-24

- Add a damped asymmetric controller to reduce overshoot.
- Add a 740 W default target and a 790 W emergency branch.
- Fail safely when either measurement source is missing or stale.
- Require and validate the EcoTracker `agePower` value.
- Account for signed AC flow when the ECOVIS consumes power.
- Reset smoothing filters after a measurement outage.
- Expose controller diagnostics through the local JSON endpoint.
