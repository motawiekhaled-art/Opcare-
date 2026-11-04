"""Message bus used by the central services.

Production: PahoBus -> Mosquitto central broker (edges bridge into it).
Development, tests and the one-process demo: InMemoryBus, which behaves like an MQTT broker
(topic wildcards + and #, retained messages, Last Will) and can also host connectors through
BusClient, the five-method client interface of the connector SDK Runtime.
"""
from __future__ import annotations

import itertools
import threading
from typing import Callable

Handler = Callable[[str, bytes], None]


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class InMemoryBus:
    def __init__(self):
        self._subs: dict[int, tuple[str, Handler]] = {}
        self._retained: dict[str, bytes] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self.published = 0

    def publish(self, topic: str, payload: bytes | str, retain: bool = False) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        with self._lock:
            self.published += 1
            if retain:
                if data:
                    self._retained[topic] = data
                else:
                    self._retained.pop(topic, None)
            targets = [h for pat, h in self._subs.values() if topic_matches(pat, topic)]
        for h in targets:
            h(topic, data)

    def subscribe(self, pattern: str, handler: Handler) -> int:
        with self._lock:
            sid = next(self._ids)
            self._subs[sid] = (pattern, handler)
            retained = [(t, d) for t, d in self._retained.items() if topic_matches(pattern, t)]
        for t, d in retained:
            handler(t, d)
        return sid

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def retained(self, topic: str) -> bytes | None:
        return self._retained.get(topic)

    def client(self) -> "BusClient":
        return BusClient(self)

    def close(self) -> None:
        pass


class BusClient:
    """Implements the SDK Runtime client interface (set_will/connect/subscribe/publish/disconnect) on a bus."""

    def __init__(self, bus: InMemoryBus):
        self.bus, self.on_message, self.will, self.subs, self.connected = bus, None, None, {}, False

    def set_will(self, topic, payload, qos, retain):
        self.will = (topic, payload, retain)

    def connect(self):
        self.connected = True

    def subscribe(self, topic, qos):
        self.subs[topic] = self.bus.subscribe(topic, lambda t, p: self.on_message(t, p))

    def unsubscribe(self, topic):
        sid = self.subs.pop(topic, None)
        if sid:
            self.bus.unsubscribe(sid)

    def publish(self, topic, payload, qos, retain):
        self.bus.publish(topic, payload, retain)

    def disconnect(self):
        for sid in self.subs.values():
            self.bus.unsubscribe(sid)
        self.subs, self.connected = {}, False

    def crash(self):
        """Simulate the process dying: the broker publishes the Last Will."""
        self.disconnect()
        if self.will:
            self.bus.publish(self.will[0], self.will[1], self.will[2])


class PahoBus:
    """Production bus over paho-mqtt 2.x to the central Mosquitto broker (mutual TLS)."""

    def __init__(self, url: str, client_id: str, username: str, password: str,
                 ca_file: str | None = None, cert_file: str | None = None, key_file: str | None = None):
        import paho.mqtt.client as mqtt
        from urllib.parse import urlparse
        self.url = urlparse(url)
        self.c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                             protocol=mqtt.MQTTv5)
        self.c.username_pw_set(username, password)
        if self.url.scheme == "mqtts":
            self.c.tls_set(ca_certs=ca_file, certfile=cert_file, keyfile=key_file)
        self._subs: dict[int, tuple[str, Handler]] = {}
        self._ids = itertools.count(1)
        self.c.on_message = self._dispatch
        self.c.on_connect = lambda c, u, f, rc, p: [c.subscribe(pat, 1) for pat, _ in self._subs.values()]
        port = self.url.port or (8883 if self.url.scheme == "mqtts" else 1883)
        self.c.connect(self.url.hostname, port, keepalive=30)
        self.c.loop_start()

    def _dispatch(self, client, userdata, msg):
        for pat, h in list(self._subs.values()):
            if topic_matches(pat, msg.topic):
                h(msg.topic, msg.payload)

    def publish(self, topic: str, payload: bytes | str, retain: bool = False) -> None:
        self.c.publish(topic, payload, qos=1, retain=retain)

    def subscribe(self, pattern: str, handler: Handler) -> int:
        sid = next(self._ids)
        self._subs[sid] = (pattern, handler)
        self.c.subscribe(pattern, 1)
        return sid

    def unsubscribe(self, sid: int) -> None:
        pat, _ = self._subs.pop(sid, (None, None))
        if pat and all(p != pat for p, _ in self._subs.values()):
            self.c.unsubscribe(pat)

    def close(self) -> None:
        self.c.disconnect()
        self.c.loop_stop()
