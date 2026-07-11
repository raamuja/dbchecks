#!/bin/bash
###############################################################################
# generate_db_dashboard.sh
#
# Wrapper that regenerates the Sybase DB/Log Size HTML dashboard from
# db_size.txt. Designed to be run manually or via cron every 2 hours.
#
# Usage:
#   ./generate_db_dashboard.sh <input_file> [output_file]
#
#   <input_file>   Path to db_size.txt (required)
#   [output_file]  Path to write the HTML dashboard
#                  (default: ./output/db_log_size_dashboard.html)
#
# Cron example (every 2 hours, on the hour):
#   0 */2 * * * /opt/dba_scripts/db_dashboard/generate_db_dashboard.sh \
#       /jags/db_size.txt \
#       /var/www/html/dashboard/db_log_size_dashboard.html \
#       >> /opt/dba_scripts/db_dashboard/cron.log 2>&1
###############################################################################

set -euo pipefail

# Resolve the directory this script lives in, so it can be called from cron
# with any working directory and still find generate_dashboard.py alongside it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/generate_dashboard.py"
LOG_FILE="${SCRIPT_DIR}/dashboard_gen.log"

INPUT_FILE="${1:-}"
OUTPUT_FILE="${2:-${SCRIPT_DIR}/output/db_log_size_dashboard.html}"

TS="$(date '+%Y-%m-%d %H:%M:%S')"

log() {
    echo "[$TS] $1" | tee -a "$LOG_FILE"
}

# --- Validate inputs -------------------------------------------------------
if [ -z "$INPUT_FILE" ]; then
    log "ERROR: No input file supplied."
    echo "Usage: $0 <input_file> [output_file]" >&2
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    log "ERROR: Input file not found: $INPUT_FILE"
    exit 1
fi

if [ ! -s "$INPUT_FILE" ]; then
    log "ERROR: Input file is empty: $INPUT_FILE"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    log "ERROR: python3 not found on PATH."
    exit 1
fi

if [ ! -f "$PY_SCRIPT" ]; then
    log "ERROR: generate_dashboard.py not found at $PY_SCRIPT"
    exit 1
fi

mkdir -p "$(dirname "$OUTPUT_FILE")"

# --- Generate ----------------------------------------------------------------
if python3 "$PY_SCRIPT" "$INPUT_FILE" "$OUTPUT_FILE" >> "$LOG_FILE" 2>&1; then
    log "SUCCESS: Dashboard generated -> $OUTPUT_FILE"
    exit 0
else
    log "ERROR: Dashboard generation failed (see $LOG_FILE for details)."
    exit 1
fi
