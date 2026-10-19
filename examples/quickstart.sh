#!/usr/bin/env bash
# End-to-end demo of the harness with a dry-run sender (nothing is sent anywhere).
set -euo pipefail
export OLIAN_DATA_DIR="${OLIAN_DATA_DIR:-./data}" OLIAN_API_KEY="${OLIAN_API_KEY:-dev-key}"
olian project create demo --name "Demo" --goal "Get 10 qualified leads" --sandbox local
olian member demo --platform cli --user me --role admin
olian sender demo dry dryrun --cap 5
olian do demo goals.add '{"title":"10 qualified leads","metric":"leads","deadline":"2026-12-31"}'
olian do demo leads.add '{"channel":"telegram","address":"@example","name":"Example"}'
olian do demo outreach.compose '{"lead_id":1,"body":"Hi! Quick question about your trip?"}'
olian do demo outreach.approve '{"id":1}'
olian do demo outreach.send_due
olian do demo project.status
olian do demo audit.list
