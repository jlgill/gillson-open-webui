#!/bin/sh
set -eu

MICROSOFT_CLIENT_SECRET="$(cat /run/secrets/owui_oauth_client_secret)"
test -n "$MICROSOFT_CLIENT_SECRET"
export MICROSOFT_CLIENT_SECRET
exec "$@"