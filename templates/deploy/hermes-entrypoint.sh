#!/bin/sh
# A heartbeat already on disk was written by another run and must not vouch for this one:
# with it gone, a run whose guard never loads has no heartbeat and the gate stays closed.
set -e
rm -f "${BRAIN_GUARD_HEARTBEAT:-/guard/heartbeat.json}"
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
