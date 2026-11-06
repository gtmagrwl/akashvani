#!/bin/bash
# Wrapper script for the Akashvani cron job.
# Appends to the log file (launchd overwrites, this script fixes that).
# Called by the LaunchAgent: com.gtmagrwl.akashvani.plist

LOG="/Users/gtmagrwl/akashvani/cron_log.txt"
PYTHON="/opt/anaconda3/envs/bank_dash/bin/python"
SCRIPT="/Users/gtmagrwl/akashvani/akashvani.py"

# Append all output to the log file
exec >> "$LOG" 2>&1

echo ""
echo "========================================================"
echo "  AKASHVANI RUN: $(date '+%a %d %b %Y %H:%M:%S %Z')"
echo "========================================================"

# Check Python binary exists
if [ ! -f "$PYTHON" ]; then
    echo "ERROR: Python binary not found at $PYTHON"
    echo "Please check that the bank_dash conda environment exists."
    exit 1
fi

# Check script exists
if [ ! -f "$SCRIPT" ]; then
    echo "ERROR: Script not found at $SCRIPT"
    exit 1
fi

# Run the script
"$PYTHON" "$SCRIPT"
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "WARNING: Script exited with code $EXIT_CODE"
fi

echo "--- Run complete ---"
