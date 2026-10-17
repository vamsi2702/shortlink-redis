"""Publish/Subscribe: clients subscribe to named channels, and the server pushes them every
message published there.

Nothing is stored: a message goes to whoever is subscribed at that moment, and is then gone. A
client that was not subscribed never sees it. (That is why the click events of this project use a
list queue instead.)
"""

from collections.abc import Callable
from typing import Protocol

from miniredis.resp import encode


class Subscriber(Protocol):
    """What the registry needs from a client."""

    subscriptions: dict[bytes, None]  # the channels it is on, in the order it joined them
    # Sends bytes to the client; returns False if it could not (the client was too slow and
    # was disconnected). None while there is no connection to push to.
    push: Callable[[bytes], bool] | None


class PubSub:
    def __init__(self) -> None:
        # channel -> the clients subscribed to it (a dict, so they keep their order and are fast
        # to add and remove)
        self._channels: dict[bytes, dict[Subscriber, None]] = {}

    def subscribe(self, client: Subscriber, channel: bytes) -> int:
        """Join a channel. Returns how many channels this client is now on."""
        self._channels.setdefault(channel, {})[client] = None
        client.subscriptions[channel] = None
        return len(client.subscriptions)

    def unsubscribe(self, client: Subscriber, channel: bytes) -> int:
        """Leave a channel (harmless if not on it). Returns how many channels are left."""
        client.subscriptions.pop(channel, None)
        subscribers = self._channels.get(channel)
        if subscribers is not None:
            subscribers.pop(client, None)
            if not subscribers:
                del self._channels[channel]
        return len(client.subscriptions)

    def unsubscribe_all(self, client: Subscriber) -> None:
        for channel in list(client.subscriptions):
            self.unsubscribe(client, channel)

    def publish(self, channel: bytes, payload: bytes) -> int:
        """Send a message to everyone on the channel. Returns how many clients got it."""
        subscribers = self._channels.get(channel)
        if not subscribers:
            return 0
        message = encode([b"message", channel, payload])  # encoded once, sent to all of them
        delivered = 0
        for client in list(subscribers):
            if client.push is not None and client.push(message):
                delivered += 1
        return delivered

    @property
    def channel_count(self) -> int:
        """How many channels have at least one subscriber."""
        return len(self._channels)

    @property
    def subscriber_count(self) -> int:
        """How many different clients are subscribed to something."""
        return len({client for subscribers in self._channels.values() for client in subscribers})
