#!/bin/bash
# Double-click in Finder. If macOS says it cannot be opened: right-click > Open > Open.
cd "$(dirname "$0")" || exit 1
exec bash "./tools/demo/start-demo.sh" "$@"
