#!/bin/sh
set -eu

export GF_AUTH_AZUREAD_CLIENT_SECRET="$(cat /run/secrets/grafana_oauth_client_secret)"
exec "$@"