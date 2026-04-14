#!/usr/bin/env python3
"""
ZMQ Publisher — standalone service that subscribes to sensor telemetry
from the EXO producer (via UDP) and re-publishes as JSON over ZMQ XPUB.

ZMQ topics:
  "sensor"  → parsed HUMI sensor frames (angles, IMU, touch)
  "raw"     → raw telemetry stats (bytes/s, recording flag)
  "joints"  → processed joint frame (Phase 2, not yet implemented)

Any subscriber (Unity, Isaac Sim, ROS2) can connect to:
  tcp://127.0.0.1:5555

Subscriber status is sent via UDP to ZMQ_STATUS_PORT (5557) → dashboard.

Usage (standalone, managed by ServiceManager):
  python core/zmq_publisher.py
"""
from __future__ import annotations

import json
import logging
import queue
import signal
import socket
import sys
import time
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    import zmq
except ImportError:
    print("ERROR: pyzmq not installed. Run: pip install pyzmq")
    sys.exit(1)

from core.config import ZMQ_PUB_ADDR, EXO_TELEMETRY_PORT, ZMQ_STATUS_PORT

log = logging.getLogger("zmq_publisher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)


class ZMQPublisher:
    """
    Listens for sensor telemetry on UDP and republishes via ZMQ XPUB.

    Uses XPUB (instead of PUB) so that subscription/unsubscription events
    are received and forwarded to the dashboard as subscriber status.
    """

    TELEMETRY_PORT = EXO_TELEMETRY_PORT

    def __init__(self, pub_addr: str = ZMQ_PUB_ADDR):
        self.pub_addr  = pub_addr
        self._running  = False
        self._ctx      = zmq.Context()
        self._pub      = self._ctx.socket(zmq.XPUB)
        # Receive ALL sub/unsub events, even repeated subscriptions
        self._pub.setsockopt(zmq.XPUB_VERBOSE, 1)

        # Thread-safe queue from UDP thread → ZMQ thread
        # Single ZMQ thread owns the socket — no lock needed
        self._send_queue: queue.Queue = queue.Queue(maxsize=2000)

        # Publish stats
        self._pub_count = 0
        self._hz        = 0.0

        # Subscriber tracking  {topic: count}
        self._sub_counts: dict[str, int] = {}
        self._sub_lock   = threading.Lock()

        # UDP status socket (→ dashboard)
        self._status_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        for attempt in range(10):
            try:
                self._pub.bind(self.pub_addr)
                break
            except zmq.ZMQError:
                if attempt == 9:
                    raise
                log.warning("ZMQ port busy, retrying in 1s… (%d/10)", attempt + 1)
                time.sleep(1)
        log.info("ZMQ XPUB bound to %s", self.pub_addr)

        self._running = True

        threading.Thread(target=self._udp_loop,   daemon=True, name="zmq-udp").start()
        threading.Thread(target=self._zmq_loop,   daemon=True, name="zmq-main").start()
        threading.Thread(target=self._stats_loop, daemon=True, name="zmq-stats").start()

        log.info("ZMQ publisher running. Topics: 'sensor', 'raw', 'joints'")
        log.info("Consumers connect to: %s", self.pub_addr)

        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._running = False
        try:
            self._pub.close()
            self._ctx.term()
        except Exception:
            pass
        try:
            self._status_sock.close()
        except Exception:
            pass
        log.info("ZMQ publisher stopped")

    def publish_raw(self, frame: dict) -> None:
        try:
            self._send_queue.put_nowait((b"raw", json.dumps(frame).encode()))
        except queue.Full:
            pass

    def publish_joints(self, frame: dict) -> None:
        try:
            self._send_queue.put_nowait((b"joints", json.dumps(frame).encode()))
        except queue.Full:
            pass

    # ── Internal ───────────────────────────────────────────────────────────────

    def _udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("0.0.0.0", self.TELEMETRY_PORT))
            log.info("Listening for sensor telemetry on UDP :%d", self.TELEMETRY_PORT)
        except OSError as exc:
            log.error("Cannot bind UDP port %d: %s", self.TELEMETRY_PORT, exc)
            return

        while self._running:
            try:
                data, _ = sock.recvfrom(65536)
                frame = json.loads(data)
                self._enrich_and_publish(frame)
            except socket.timeout:
                continue
            except json.JSONDecodeError:
                continue
            except Exception as exc:
                log.debug("UDP recv error: %s", exc)

        sock.close()

    def _enrich_and_publish(self, frame: dict) -> None:
        frame.setdefault("ts", time.time())
        frame.setdefault("source", "glove-core")

        if frame.get("type") == "sensor_frame":
            topic = b"sensor"
        else:
            topic = b"raw"

        try:
            self._send_queue.put_nowait((topic, json.dumps(frame).encode()))
        except queue.Full:
            pass

    def _zmq_loop(self) -> None:
        """
        Single thread that owns the XPUB socket.

        Drains _send_queue to publish outgoing frames, then polls for
        subscription/unsubscription events. No other thread touches self._pub.
        """
        poller = zmq.Poller()
        poller.register(self._pub, zmq.POLLIN)

        while self._running:
            # Drain pending sends first
            while True:
                try:
                    topic, data = self._send_queue.get_nowait()
                    self._pub.send_multipart([topic, data], zmq.NOBLOCK)
                    self._pub_count += 1
                except queue.Empty:
                    break
                except zmq.ZMQError as exc:
                    log.warning("ZMQ send error: %s", exc)
                    break

            # Poll for subscription events (10 ms so sends stay responsive)
            try:
                events = dict(poller.poll(10))
                if self._pub not in events:
                    continue

                msg = self._pub.recv(zmq.NOBLOCK)
                if not msg:
                    continue

                subscribed = msg[0] == 1
                topic = msg[1:].decode("utf-8", errors="replace")

                with self._sub_lock:
                    prev = self._sub_counts.get(topic, 0)
                    if subscribed:
                        self._sub_counts[topic] = prev + 1
                        log.info("ZMQ subscriber joined   topic='%s'  count=%d",
                                 topic, self._sub_counts[topic])
                    else:
                        self._sub_counts[topic] = max(0, prev - 1)
                        log.info("ZMQ subscriber left     topic='%s'  count=%d",
                                 topic, self._sub_counts[topic])

            except zmq.Again:
                continue
            except Exception as exc:
                if self._running:
                    log.debug("ZMQ loop error: %s", exc)

    def _stats_loop(self) -> None:
        prev = 0
        while self._running:
            time.sleep(2)
            delta = self._pub_count - prev
            prev  = self._pub_count
            self._hz = delta / 2.0

            with self._sub_lock:
                sub_counts = dict(self._sub_counts)

            total = sum(sub_counts.values())

            log.info("ZMQ rate: %.1f Hz  frames=%d  subscribers=%d %s",
                     self._hz, self._pub_count, total,
                     {k: v for k, v in sub_counts.items() if v > 0})

            # Send status to dashboard via UDP
            status = {
                "type":               "zmq_status",
                "total_subscribers":  total,
                "topics":             {k: v for k, v in sub_counts.items() if v >= 0},
                "pub_hz":             round(self._hz, 1),
                "total_frames":       self._pub_count,
            }
            try:
                self._status_sock.sendto(
                    json.dumps(status).encode(),
                    ("127.0.0.1", ZMQ_STATUS_PORT),
                )
            except Exception:
                pass


# ── Standalone entry point ──────────────────────────────────────────────────
def main() -> None:
    pub = ZMQPublisher()

    def _shutdown(sig, frame):
        pub.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    pub.start()


if __name__ == "__main__":
    main()
