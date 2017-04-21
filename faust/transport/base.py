"""Base message transport implementation."""
import asyncio
import weakref
from itertools import count
from typing import (
    Any, Awaitable, Callable, ClassVar,
    Iterator, MutableMapping, Optional, List, Sequence, Type, cast,
)
from ..types import AppT, Message, TopicPartition
from ..types.transports import (
    ConsumerCallback, ConsumerT, MessageRefT, ProducerT, TransportT,
)
from ..utils.services import Service

__all__ = ['MessageRef', 'Consumer', 'Producer', 'Transport']

# The Transport is responsible for:
#
#  - Holds reference to the app that created it.
#  - Creates new consumers/producers.
#
# The Consumer is responsible for:
#
#   - Holds reference to the transport that created it
#   - ... and the app via ``self.transport.app``.
#   - Has a callback that usually points back to ``StreamManager.on_message``.
#   - Receives messages and calls the callback for every message received.
#   - Keeps track of the message and it's acked/unacked status.
#   - If automatic acks are enabled the message is acked when the Message goes
#     out of scope (like any variable using reference counting).
#   - The StreamManager forwards the message to all Streams that subscribes
#     to the topic the message was sent to.
#      - Each individual Stream will deserialize the message and create
#        one Event instance per stream.  The message goes out of scope (and so
#        acked), when all events referencing the message is also out of scope.
#   - Commits the offset at an interval
#      - The current offset is based on range of the messages acked.
#
# The Producer is responsible for:
#
#   - Holds reference to the transport that created it
#   - ... and the app via ``self.transport.app``.
#   - Sending messages.
#
# To see a reference transport implementation go to:
#     faust/transport/aiokafka.py

PartitionsRevokedCallback = Callable[[Sequence[TopicPartition]], None]
PartitionsAssignedCallback = Callable[[Sequence[TopicPartition]], None]


class MessageRef(weakref.ref, MessageRefT):
    """Weak-reference to :class:`~faust.types.Message`.

    Remembers the offset of the message, even after message is out of scope.
    """

    # Used for tracking when messages go out of scope.

    def __init__(self, message: Message,
                 callback: Callable = None,
                 tp: TopicPartition = None,
                 offset: int = None,
                 consumer_id: int = None) -> None:
        super().__init__(message, callback)
        self.tp = tp
        self.offset = offset
        self.consumer_id = consumer_id


class Consumer(Service, ConsumerT):
    """Base Consumer."""

    RebalanceListener: ClassVar[Type]

    #: This counter generates new consumer ids.
    _consumer_ids: ClassVar[Iterator[int]] = count(0)

    _app: AppT
    _dirty_messages: List[MessageRefT] = None
    _acked: MutableMapping[TopicPartition, List[int]] = None
    _current_offset: MutableMapping[TopicPartition, int] = None
    _recently_acked: asyncio.Queue

    def __init__(self, transport: TransportT,
                 *,
                 callback: ConsumerCallback = None,
                 on_partitions_revoked: PartitionsRevokedCallback = None,
                 on_partitions_assigned: PartitionsAssignedCallback = None,
                 autoack: bool = True,
                 commit_interval: float = None,
                 **kwargs: Any) -> None:
        assert callback is not None
        self.id = next(self._consumer_ids)
        self.transport = transport
        self._app = self.transport.app
        self.callback = callback
        self.autoack = autoack
        self._on_message_in = self._app.on_message_in
        self._on_partitions_revoked = on_partitions_revoked
        self._on_partitions_assigned = on_partitions_assigned
        self.commit_interval = (
            commit_interval or self._app.commit_interval)
        self._dirty_messages = []
        self._acked = {}
        self._current_offset = {}
        self._commit_mutex = asyncio.Lock(loop=self.loop)
        self._rebalance_listener = self.RebalanceListener(self)
        self._recently_acked = asyncio.Queue(loop=self.transport.loop)
        super().__init__(loop=self.transport.loop, **kwargs)

    async def register_timers(self) -> None:
        self.add_future(self._recently_acked_handler())
        self.add_future(self._commit_handler())

    async def track_message(self, message: Message, offset: int) -> None:
        _id = self.id
        tp = self._new_topicpartition(message.topic, message.partition)
        if tp not in self._acked:
            self._acked[tp] = []
        # keep weak reference to message, to be notified when out of scope.
        self._dirty_messages.append(
            MessageRef(message, self.on_message_ready,
                       tp=TopicPartition(message.topic, message.partition),
                       offset=offset,
                       consumer_id=self.id))
        # call sensors
        await self._on_message_in(_id, tp, offset, message)

    def on_message_ready(self, ref: MessageRefT) -> None:
        if self.autoack:
            self.ack(ref.tp, ref.offset)

    def ack(self, tp: TopicPartition, offset: int) -> None:
        acked_for_tp = self._acked[tp]
        acked_for_tp.append(offset)
        acked_for_tp.sort()
        self._recently_acked.put((tp, offset))

    async def _commit_handler(self) -> None:
        await asyncio.sleep(self.commit_interval)
        while 1:
            await self.maybe_commit()
            await asyncio.sleep(self.commit_interval)

    async def _recently_acked_handler(self) -> None:
        get = self._recently_acked.get
        on_message_out = self._app.on_message_out
        while not self.should_stop:
            tp, offset = await get()
            await on_message_out(self.id, tp, offset, None)

    async def maybe_commit(self) -> bool:
        async with self._commit_mutex:
            offsets = {}
            for tp in self._acked:
                try:
                    offset = self._new_offset(tp)
                except IndexError:
                    pass
                else:
                    if self._should_commit(tp, offset):
                        self._current_offset[tp] = offset
                        meta = self._get_topic_meta(tp.topic)
                        offsets[tp] = self._new_offsetandmetadata(offset, meta)
            if offsets:
                await self._commit(offsets)
                return True
            return False

    def _get_topic_meta(self, topic: str) -> Any:
        raise NotImplementedError()

    def _new_topicpartition(
            self, topic: str, partition: int) -> TopicPartition:
        raise NotImplementedError()

    def _new_offsetandmetadata(self, offset: int, meta: Any) -> Any:
        raise NotImplementedError()

    async def on_task_error(self, exc: Exception) -> None:
        if self.autoack:
            await self.maybe_commit()

    def _should_commit(self, tp: TopicPartition, offset: int) -> bool:
        return (
            tp not in self._current_offset or
            (bool(offset) and offset > self._current_offset[tp])
        )

    def _new_offset(self, tp: TopicPartition) -> int:
        acked = self._acked[tp]
        for i, offset in enumerate(acked):
            if offset != acked[i - 1]:
                break
        else:
            raise IndexError()
        return offset


class Producer(Service, ProducerT):
    """Base Producer."""

    def __init__(self, transport: TransportT, **kwargs: Any) -> None:
        self.transport = transport
        super().__init__(loop=self.transport.loop, **kwargs)

    async def send(
            self,
            topic: str,
            key: Optional[bytes],
            value: Optional[bytes]) -> Awaitable:
        raise NotImplementedError()

    async def send_and_wait(
            self,
            topic: str,
            key: Optional[bytes],
            value: Optional[bytes]) -> Awaitable:
        raise NotImplementedError()


class Transport(TransportT):
    """Message transport implementation."""

    #: Consumer subclass used for this transport.
    Consumer: ClassVar[Type]

    #: Producer subclass used for this transport.
    Producer: ClassVar[Type]

    driver_version: str

    def __init__(self, url: str, app: AppT,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.url = url
        self.app = app
        self.loop = loop

    def create_consumer(self, callback: ConsumerCallback,
                        **kwargs: Any) -> ConsumerT:
        return cast(ConsumerT, self.Consumer(
            self, callback=callback, **kwargs))

    def create_producer(self, **kwargs: Any) -> ProducerT:
        return cast(ProducerT, self.Producer(self, **kwargs))
