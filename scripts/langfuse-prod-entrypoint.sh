#!/bin/sh
set -eu

export AUTH_AZURE_AD_CLIENT_SECRET="$(cat /run/secrets/langfuse_oauth_client_secret)"
exec /bin/sh /app/web/entrypoint.sh "$@"