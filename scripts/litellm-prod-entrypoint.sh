#!/bin/sh
set -eu

export MICROSOFT_CLIENT_SECRET="$(cat /run/secrets/litellm_oauth_client_secret)"
exec /app/docker/prod_entrypoint.sh "$@"