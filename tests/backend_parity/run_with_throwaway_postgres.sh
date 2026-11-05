#!/usr/bin/env bash
# Run the PostgreSQL-vs-SQLite parity suite against a throwaway PostgreSQL.
#
#   tests/backend_parity/run_with_throwaway_postgres.sh [--collation C|en_US.UTF-8] [pytest args...]
#
# Needs initdb/pg_ctl/openssl on PATH. Starts a server on a free port with
# SSL (the app requires sslmode=require), runs the suite, stops the server
# and deletes its data directory. The default C collation makes PostgreSQL
# sort text by byte order like SQLite, so every response must match exactly;
# with a locale collation the reference lists are compared order-insensitively.
set -euo pipefail
collation="C"
if [[ "${1:-}" == "--collation" ]]; then collation="$2"; shift 2; fi
here="$(cd "$(dirname "$0")" && pwd)"
data="$(mktemp -d)"
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cleanup() { pg_ctl -D "$data/pg" -m fast stop >/dev/null 2>&1 || true; rm -rf "$data"; }
trap cleanup EXIT
initdb -D "$data/pg" -U lakemeter --auth=trust --encoding=UTF8 --locale="$collation" >/dev/null
openssl req -new -x509 -days 2 -noenc -subj "/CN=localhost" \
  -keyout "$data/pg/server.key" -out "$data/pg/server.crt" >/dev/null 2>&1
chmod 600 "$data/pg/server.key"
printf "port = %s\nssl = on\nlisten_addresses = 'localhost'\nunix_socket_directories = '%s'\n" \
  "$port" "$data" >> "$data/pg/postgresql.conf"
pg_ctl -D "$data/pg" -l "$data/pg.log" -w start >/dev/null
createdb -h localhost -p "$port" -U lakemeter lakemeter_parity
export LAKEMETER_PARITY_PG_URL="postgresql+psycopg://lakemeter@localhost:${port}/lakemeter_parity?sslmode=require"
cd "$here/../.."
python -m pytest tests/backend_parity -p no:cacheprovider "$@"
