#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  python3 launcher.py
  result=$?
else
  echo "Please install Python 3.11 or newer from https://www.python.org/downloads/"
  result=1
fi
if [ "$result" -ne 0 ]; then
  read -r -p "Press Return to close this window. "
fi
exit "$result"
