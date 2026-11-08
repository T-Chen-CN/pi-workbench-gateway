"""中继：把云端的任务帧推给设备的常连 SSE。

一台设备可以有多条订阅（重连时新旧并存几秒），所以是**扇出**而不是单槽。
队列满就丢帧而不是阻塞调用方：丢掉的帧由「重连时补发 queued 任务」兜底，
宁可让设备重连后补课，也不能让云端的一次入队卡在一个不在看的连接上。
"""

from __future__ import annotations

import queue
import threading

MAX_QUEUE = 64


class Relay:
    def __init__(self, *, max_queue: int = MAX_QUEUE) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = {}
        self._max = max_queue

    def subscribe(self, device_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=self._max)
        with self._lock:
            self._subs.setdefault(device_id, []).append(channel)
        return channel

    def unsubscribe(self, device_id: str, channel: queue.Queue) -> None:
        with self._lock:
            channels = self._subs.get(device_id)
            if not channels:
                return
            if channel in channels:
                channels.remove(channel)
            if not channels:
                self._subs.pop(device_id, None)

    def publish(self, device_id: str, frame: dict) -> int:
        """返回投递到的连接数；0 表示设备此刻没有中继在线（任务还在库里排队）。"""
        with self._lock:
            channels = list(self._subs.get(device_id, ()))
        delivered = 0
        for channel in channels:
            try:
                channel.put_nowait(frame)
                delivered += 1
            except queue.Full:
                continue
        return delivered

    def online(self, device_id: str) -> bool:
        with self._lock:
            return bool(self._subs.get(device_id))

    def subscribers(self, device_id: str) -> int:
        with self._lock:
            return len(self._subs.get(device_id, ()))

    def drop(self, device_id: str) -> None:
        """设备被撤销：直接掐掉它的中继（下一次重连就会被 401 拦下）。"""
        with self._lock:
            self._subs.pop(device_id, None)
