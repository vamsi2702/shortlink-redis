"""The INFO command: a text report of how the server is doing.

The layout is real Redis's: a "# Section" line, then one "field:value" line per fact, sections
separated by a blank line and always in the same order. Clients such as redis-py read it.
"""

import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miniredis.commands import Context

VERSION = "0.1.0"
SECTIONS = ("server", "clients", "memory", "stats", "keyspace")
_ALL = {"default", "all", "everything"}


def human_bytes(count: int) -> str:
    """12345 -> '12.06K', the way Redis shows memory."""
    if count < 1024:
        return f"{count}B"
    value = float(count)
    for unit in "KMGT":
        value /= 1024
        if value < 1024 or unit == "T":
            return f"{value:.2f}{unit}"
    raise AssertionError("unreachable")


def _wanted(requested: list[str]) -> set[str]:
    names = {name.lower() for name in requested}
    if not names or names & _ALL:
        return set(SECTIONS)
    return names & set(SECTIONS)  # section names we do not have are ignored


def _facts(name: str, ctx: "Context") -> dict[str, object]:
    stats, store = ctx.stats, ctx.store
    if name == "server":
        uptime = int(time.time() - stats.started_at)
        return {
            "miniredis_version": VERSION,
            "redis_mode": "standalone",
            "process_id": os.getpid(),
            "uptime_in_seconds": uptime,
            "uptime_in_days": uptime // 86400,
        }
    if name == "clients":
        return {
            "connected_clients": stats.connected_clients,
            "pubsub_clients": ctx.pubsub.subscriber_count,
        }
    if name == "memory":
        used = store.memory_estimate()
        return {"used_memory": used, "used_memory_human": human_bytes(used)}
    if name == "stats":
        return {
            "total_connections_received": stats.connections_received,
            "total_commands_processed": stats.commands_processed,
            "expired_keys": store.expired_keys,
            "keyspace_hits": stats.keyspace_hits,
            "keyspace_misses": stats.keyspace_misses,
            "pubsub_channels": ctx.pubsub.channel_count,
        }
    if name == "keyspace" and len(store) > 0:
        summary = f"keys={len(store)},expires={store.expiring_count()}"
        return {"db0": f"{summary},avg_ttl={store.average_ttl_ms()}"}
    return {}


def render(ctx: "Context", requested: list[str]) -> str:
    """The report for the requested sections (all of them if none are named)."""
    wanted = _wanted(requested)
    extra = ctx.extra_info() if ctx.extra_info is not None else {}
    blocks = []
    for name in SECTIONS:  # always in this order, whatever order they were asked for
        if name not in wanted:
            continue
        facts = _facts(name, ctx) | extra.get(name, {})
        lines = "".join(f"{field}:{value}\r\n" for field, value in facts.items())
        blocks.append(f"# {name.capitalize()}\r\n{lines}")
    return "\r\n".join(blocks)
