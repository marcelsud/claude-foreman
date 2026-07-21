from __future__ import annotations

import atexit
import json
import os
import socket
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any


SocketFactory = Callable[..., socket.socket]


class LocalWake:
    """Best-effort local wake hints backed by private Unix datagram sockets.

    Wake hints deliberately contain no durable state. Callers must always re-read
    SQLite after subscribing or waking, so lost datagrams and process restarts are
    harmless.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        recovery_interval: float = 1.0,
        socket_factory: SocketFactory = socket.socket,
    ) -> None:
        self.wake_dir = Path(data_dir) / "wake"
        self.recovery_interval = max(0.05, float(recovery_interval))
        self._socket_factory = socket_factory
        self._condition = threading.Condition()
        self._generations: dict[tuple[str, str | None], int] = {}
        self._listener: socket.socket | None = None
        self._listener_path: Path | None = None
        self._listener_thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self.ipc_available = False
        atexit.register(self.close)

    @property
    def closed(self) -> bool:
        return self._closed

    def subscribe(self, channel: str = "events", key: str | None = None) -> int:
        """Register before reading durable state and return a race token."""
        self._ensure_listener()
        with self._condition:
            return self._generations.get((channel, key), 0)

    def wait(
        self,
        generation: int,
        timeout: float,
        *,
        channel: str = "events",
        key: str | None = None,
    ) -> int:
        """Wait for a matching hint or timeout and return the latest token."""
        deadline = monotonic() + max(0.0, float(timeout))
        topic = (channel, key)
        with self._condition:
            while generation == self._generations.get(topic, 0) and not self._closed:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self._generations.get(topic, 0)

    def publish(
        self,
        *,
        channel: str = "events",
        key: str | None = None,
        event_id: int | None = None,
    ) -> None:
        """Publish minimal identifiers; receivers must re-read SQLite."""
        self._signal(channel, key)
        if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
            return
        try:
            endpoints = tuple(self.wake_dir.glob("*.sock"))
        except OSError:
            return
        if not endpoints:
            return
        payload = json.dumps(
            {"channel": channel, "key": key, "event_id": event_id},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        try:
            sender = self._socket_factory(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError:
            return
        try:
            for endpoint in endpoints:
                if endpoint == self._listener_path:
                    continue
                try:
                    sender.sendto(payload, str(endpoint))
                except (FileNotFoundError, ConnectionRefusedError):
                    # Crashed processes can leave stale socket paths. Removing a
                    # path with a random per-process name cannot affect a new peer.
                    try:
                        endpoint.unlink()
                    except OSError:
                        pass
                except OSError:
                    continue
        finally:
            sender.close()

    def close(self) -> None:
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            listener = self._listener
            path = self._listener_path
            listener_thread = self._listener_thread
            self._listener = None
            self._listener_path = None
            self._listener_thread = None
            self.ipc_available = False
        with self._condition:
            self._condition.notify_all()
        if listener is not None:
            listener.close()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
        if listener_thread is not None and listener_thread is not threading.current_thread():
            listener_thread.join(timeout=0.6)

    def _ensure_listener(self) -> None:
        if self._listener is not None or self._closed:
            return
        with self._start_lock:
            if self._listener is not None or self._closed:
                return
            if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
                return
            listener: socket.socket | None = None
            path: Path | None = None
            try:
                self.wake_dir.mkdir(parents=True, exist_ok=True)
                if self.wake_dir.is_symlink():
                    raise OSError("wake directory must not be a symlink")
                self.wake_dir.chmod(0o700)
                path = self.wake_dir / f"{os.getpid()}-{uuid.uuid4().hex[:10]}.sock"
                listener = self._socket_factory(socket.AF_UNIX, socket.SOCK_DGRAM)
                listener.settimeout(0.5)
                listener.bind(str(path))
                try:
                    path.chmod(0o600)
                except OSError:
                    # The containing 0700 directory remains the access boundary
                    # on platforms that disallow chmod on Unix socket nodes.
                    pass
            except (OSError, ValueError):
                if listener is not None:
                    listener.close()
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return
            self._listener = listener
            self._listener_path = path
            self.ipc_available = True
            self._listener_thread = threading.Thread(
                target=self._listen,
                name="foreman-local-wake",
                daemon=True,
            )
            self._listener_thread.start()

    def _listen(self) -> None:
        while not self._closed:
            listener = self._listener
            if listener is None:
                return
            try:
                raw = listener.recv(512)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                message: Any = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            if set(message) - {"channel", "key", "event_id"}:
                continue
            channel = message.get("channel")
            key = message.get("key")
            if not isinstance(channel, str) or (key is not None and not isinstance(key, str)):
                continue
            self._signal(channel, key)

    def _signal(self, channel: str, key: str | None) -> None:
        with self._condition:
            topic = (channel, key)
            self._generations[topic] = self._generations.get(topic, 0) + 1
            self._condition.notify_all()
