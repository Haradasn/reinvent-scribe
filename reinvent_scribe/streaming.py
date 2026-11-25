"""AssemblyAI Universal-Streaming client (low-latency online captions).

Speaks the v3 realtime protocol over WebSocket: send 16kHz mono PCM16
binary frames, receive JSON Turn events. Finalized turns go to on_segment,
partial turns to on_partial. Any failure raises from start()/feed() so the
caller can fall back to the local Whisper transcriber.
"""
import json
import logging
import socket
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

_WSS = "wss://streaming.assemblyai.com/v3/ws?sample_rate=16000&format_turns=true"


class AssemblyAIStreamer:
    def __init__(self, api_key: str,
                 on_segment: Callable[[float, str], None],
                 on_partial: Callable[[str], None] = lambda t: None):
        self.api_key = api_key
        self.on_segment = on_segment
        self.on_partial = on_partial
        self._ws = None
        self._receiver: threading.Thread | None = None
        self._buf = bytearray()
        self._terminated = threading.Event()
        self.confirmed_sec = 0.0  # audio up to here is in final captions
        self._last_ok = time.monotonic()
        self._stopping = False
        self._split_turn = None  # turn already asked to end

    # AAI v3 rejects audio chunks shorter than 50ms; batch to >=80ms
    _MIN_SEND_BYTES = int(16000 * 0.08) * 2

    def start(self) -> None:
        from websockets.sync.client import connect
        self._ws = connect(_WSS,
                           additional_headers={"Authorization": self.api_key},
                           open_timeout=5)
        self._receiver = threading.Thread(target=self._recv_loop, daemon=True,
                                          name="aai-receiver")
        self._receiver.start()
        self._last_ok = time.monotonic()
        for fn in (self._ping_loop, self._watchdog):
            threading.Thread(target=fn, daemon=True, name="aai-watch").start()

    # A vanished network does not fail send(): the OS buffers for a while and
    # then blocks. So the link is proven by pongs, and cut when they stop.
    _PING_SEC = 2.0
    _DEAD_SEC = 8.0

    def _ping_loop(self) -> None:
        while not self._terminated.is_set() and not self._stopping:
            try:
                if self._ws.ping().wait(self._DEAD_SEC):
                    self._last_ok = time.monotonic()
            except Exception:
                return
            time.sleep(self._PING_SEC)

    def _watchdog(self) -> None:
        while not self._terminated.is_set() and not self._stopping:
            if time.monotonic() - self._last_ok > self._DEAD_SEC:
                log.warning("AssemblyAI not responding; cutting the connection")
                self._cut()
                return
            time.sleep(0.5)

    def _cut(self) -> None:
        self._terminated.set()
        try:  # unlike close(), this also frees a send() that is blocked
            self._ws.socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    def alive(self) -> bool:
        return not self._terminated.is_set()

    def _recv_loop(self) -> None:
        try:
            for message in self._ws:
                if isinstance(message, str):
                    self._handle(message)
            log.info("AssemblyAI connection closed")
        except Exception as e:
            log.warning("AssemblyAI receiver closed: %r", e)
        finally:
            self._terminated.set()

    def _handle(self, message: str) -> None:
        try:
            msg = json.loads(message)
        except ValueError:
            return
        if msg.get("type") != "Turn":
            if msg.get("type") not in ("Begin", "SpeechStarted", "Termination"):
                log.warning("AssemblyAI message: %s", message[:300])
            if msg.get("type") == "Termination":
                self._terminated.set()
            return
        transcript = (msg.get("transcript") or "").strip()
        if not transcript:
            return
        if msg.get("end_of_turn"):
            words = msg.get("words") or []
            start_sec = (words[0].get("start", 0) / 1000.0) if words else 0.0
            self.on_segment(start_sec, transcript)
            if words:
                self.confirmed_sec = words[-1].get("end", 0) / 1000.0
        else:
            self.on_partial(transcript)
            self._split_long_turn(msg, transcript)

    # A speaker who never pauses would otherwise hold one turn open for 40s+,
    # and translation only starts once the turn is final.
    _SOFT_TURN_MS = 8000    # end at the next sentence boundary
    _HARD_TURN_MS = 20000   # end regardless

    def _split_long_turn(self, msg: dict, transcript: str) -> None:
        words = msg.get("words") or []
        turn = msg.get("turn_order")
        if not words or turn is None or turn == self._split_turn or self._ws is None:
            return
        length = words[-1].get("end", 0) - words[0].get("start", 0)
        if length >= self._HARD_TURN_MS or (
                length >= self._SOFT_TURN_MS and transcript[-1] in ".?!"):
            self._split_turn = turn
            try:
                self._ws.send(json.dumps({"type": "ForceEndpoint"}))
            except Exception:
                log.info("could not split a long turn")

    def feed(self, pcm: np.ndarray) -> None:
        if self._terminated.is_set():
            raise ConnectionError("stream closed")
        self._buf.extend(np.asarray(pcm, dtype=np.int16).tobytes())
        if len(self._buf) >= self._MIN_SEND_BYTES:
            data, self._buf = bytes(self._buf), bytearray()
            self._ws.send(data)

    def _flush_audio(self) -> None:
        if self._buf:  # pad to the 50ms minimum the service accepts
            data = bytes(self._buf).ljust(1600, b"\0")
            self._buf = bytearray()
            self._ws.send(data)

    def end_utterance(self) -> None:
        """Finalize what was said so far (used when pausing)."""
        self._flush_audio()
        self._ws.send(json.dumps({"type": "ForceEndpoint"}))

    def stop(self) -> None:
        self._stopping = True
        try:
            if self._ws is not None and not self._terminated.is_set():
                self._flush_audio()
                self._ws.send(json.dumps({"type": "Terminate"}))
                # closing right away would drop the last utterance
                self._terminated.wait(timeout=4)
        except Exception:
            log.info("streamer already disconnected")
        finally:
            if self._ws is not None:
                try:
                    if self._terminated.is_set():
                        self._cut()
                    self._ws.close()
                except Exception:
                    pass
        if self._receiver is not None:
            self._receiver.join(timeout=3)
