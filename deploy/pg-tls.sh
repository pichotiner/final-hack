#!/bin/sh
set -eu
mkdir -p /var/lib/postgresql/tls
cp /run/pg-tls/server.crt /var/lib/postgresql/tls/server.crt
cp /run/pg-tls/server.key /var/lib/postgresql/tls/server.key
chown postgres:postgres /var/lib/postgresql/tls/*
chmod 600 /var/lib/postgresql/tls/server.key
exec /usr/local/bin/docker-entrypoint.sh "$@"
