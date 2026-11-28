#!/usr/bin/env bash
# Test-only: builds the IEC61850bean MMS server (Apache-2.0) and tools/interop/Iec61850BeanServer.java into $1
# (default ./.interop/iec61850bean) for the CI job "iec61850-interop". Nothing of it is part of the product.
# Prints the Java classpath to use. Needs git and a JDK (11+).
set -euo pipefail
out="${1:-.interop/iec61850bean}"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$out/src" "$out/classes" "$out/stub/org/slf4j"
[ -d "$out/src/iec61850bean" ] || git -c advice.detachedHead=false clone -q --depth 1 --branch v1.9.0 https://github.com/beanit/iec61850bean "$out/src/iec61850bean"
[ -d "$out/src/asn1bean" ] || git -c advice.detachedHead=false clone -q --depth 1 --branch v1.12.0 https://github.com/beanit/asn1bean "$out/src/asn1bean"
# a do-nothing slf4j API, so no logging jar has to be fetched
cat > "$out/stub/org/slf4j/Logger.java" <<'J'
package org.slf4j;
public interface Logger { default void debug(String s, Object... a){} default void info(String s, Object... a){} default void warn(String s, Object... a){} default void error(String s, Object... a){} default void trace(String s, Object... a){} default boolean isDebugEnabled(){return false;} default boolean isTraceEnabled(){return false;} }
J
cat > "$out/stub/org/slf4j/LoggerFactory.java" <<'J'
package org.slf4j;
public class LoggerFactory { public static Logger getLogger(Class<?> c){ return new Logger(){}; } public static Logger getLogger(String n){ return new Logger(){}; } }
J
{ find "$out/src/asn1bean" -path '*src/main/java*' -name '*.java' | grep -v '/compiler/'
  find "$out/src/iec61850bean/src/main/java" "$out/src/iec61850bean/src/main/java-gen" -name '*.java' | grep -v '/gui/\|/clientgui/\|/app/'
  find "$out/stub" -name '*.java'
  echo "$here/Iec61850BeanServer.java"; } > "$out/files.txt"
javac -nowarn -d "$out/classes" @"$out/files.txt" 2>&1 | grep -v "^Picked up\|^Note:" || true
test -f "$out/classes/Iec61850BeanServer.class"
echo "$out/classes"
