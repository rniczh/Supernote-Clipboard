#!/bin/bash
# Double-click in Finder to start Supernote Clipboard.
cd "$(dirname "$0")"
exec /usr/bin/env python3 sn_clipboard.py "$@"
