#!/usr/bin/env bash
# Builds lib60870's unmodified example IEC 104 server (cs104_server) from the upstream sources, for the IEC 104
# interoperability test only (CI job iec104-interop). lib60870 is GPLv3: it is downloaded at test time into the given
# directory and never committed to this repository or put into any image. Prints the path of the server binary.
#   tools/interop/build_lib60870_server.sh /tmp/lib60870
set -euo pipefail
dir="${1:-.interop/lib60870}"
version="v2.4.1"
if [ ! -d "$dir/src" ]; then
  mkdir -p "$dir"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$version" https://github.com/mz-automation/lib60870 "$dir/src"
fi
cmake -S "$dir/src/lib60870-C" -B "$dir/build" -DBUILD_TESTS=OFF > /dev/null
cmake --build "$dir/build" --target cs104_server -j 4 > /dev/null
echo "$(cd "$dir/build/examples/cs104_server" && pwd)/cs104_server"
