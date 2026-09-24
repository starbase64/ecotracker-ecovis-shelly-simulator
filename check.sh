#!/bin/sh
set -eu

printf '%s\n' '--- Containers ---'
docker compose ps

printf '\n%s\n' '--- Limiter ---'
curl --fail --silent --show-error 'http://127.0.0.1:18081/v1/json' |
  python3 -m json.tool

printf '\n%s\n' '--- Simulated Shelly Pro 3EM ---'
curl --fail --silent --show-error 'http://127.0.0.1/rpc/EM.GetStatus?id=0' |
  python3 -m json.tool

printf '\n%s\n' '--- UDP port 8888 ---'
ss -lunp 'sport = :8888' || true
