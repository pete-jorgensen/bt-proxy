"""The API server half of link ownership.

The manager can only release a client's links if the server tells it who asked
and when they left. These tests cover that wiring, which is the deterministic
mechanism — the sweeper behind it is only a backstop.
"""

from __future__ import annotations

import asyncio

import pytest

from bt_proxy import proto
from bt_proxy.api_server import APIConnection, APIServer

# Locally-administered addresses (the 0x02 bit is set): never a real vendor's device.
ADDRESS = 0x020000000001
OTHER_ADDRESS = 0x020000000002

pytestmark = pytest.mark.asyncio


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, *_a, **_kw):
        return ("127.0.0.1", 12345)


def _connect_request(address: int) -> bytes:
    """A BluetoothDeviceRequest: field 1 = address, field 2 = request type.

    Both varints, so the tags are (field << 3) | 0 — 0x08 and 0x10.
    """
    return (
        b"\x08"
        + proto.encode_varint(address)
        + b"\x10"
        + proto.encode_varint(proto.BLE_REQUEST_CONNECT_V3_WITH_CACHE)
    )


def make_connection(server) -> APIConnection:
    conn = APIConnection(asyncio.StreamReader(), FakeWriter(), server)
    server._connections.append(conn)
    return conn


@pytest.fixture
def server(manager):
    return APIServer(manager, name="test-proxy", port=0)


async def test_a_client_going_away_releases_its_links(
    server, manager, fake_bleak
):
    """The primary mechanism: no client, no link."""
    conn = make_connection(server)
    await manager.connect_device(ADDRESS, owner=conn)
    assert manager._connections[ADDRESS].connected

    await conn._cleanup()

    assert ADDRESS not in manager._connections
    assert all(not c.is_connected for c in fake_bleak.instances)


async def test_one_client_leaving_does_not_disturb_another(
    server, manager, fake_bleak
):
    """A departing client must take its own links and nothing else."""
    leaving = make_connection(server)
    staying = make_connection(server)
    await manager.connect_device(ADDRESS, owner=leaving)
    await manager.connect_device(OTHER_ADDRESS, owner=staying)

    await leaving._cleanup()

    assert ADDRESS not in manager._connections
    assert manager._connections[OTHER_ADDRESS].connected, (
        "released a link belonging to a client that is still here"
    )


async def test_liveness_reports_only_current_open_clients(server, manager):
    """What the sweeper asks, and the two answers it must get right."""
    live = make_connection(server)
    gone = make_connection(server)
    gone._closed = True

    assert server.is_live_connection(live) is True
    assert server.is_live_connection(gone) is False
    assert server.is_live_connection(object()) is False, (
        "an unknown token must read as gone, not as live"
    )


async def test_start_teaches_the_manager_how_to_ask(server, manager, monkeypatch):
    """Without this wiring the sweeper can never act, and would sit inert."""
    assert manager._owner_is_live is None

    async def fake_start_server(*_a, **_kw):
        class _S:
            def close(self):
                pass

            async def wait_closed(self):
                return None

        return _S()

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    await server.start()

    assert manager._owner_is_live is not None
    conn = make_connection(server)
    assert manager._owner_is_live(conn) is True


async def test_a_connect_finishing_after_the_client_left_is_released(
    server, manager, fake_bleak
):
    """The server-side guard, matching the manager-side one.

    The client's socket dies while a connect is in flight. Whatever the radio
    establishes must go, not sit there holding the device off the air.
    """
    conn = make_connection(server)
    fake_bleak.gate = asyncio.Event()

    request = _connect_request(ADDRESS)
    task = asyncio.ensure_future(conn._handle_ble_device_request(request))
    await fake_bleak.wait_for_attempt(1)

    # The client vanishes mid-connect.
    conn._closed = True
    fake_bleak.gate.set()
    await task

    assert ADDRESS not in manager._connections
    assert all(not c.is_connected for c in fake_bleak.instances)
