"""A GATT link must not outlive the request that asked for it.

A connect retry can land seconds after the client has stopped waiting for it.
If bt-proxy keeps that link, then — because a connected
BLE peripheral stops advertising — the device goes silent to every proxy,
including the one holding it, and nothing in the process will ever release it.
It stays that way while powered and in range, until the link is released.

Each test below names the specific way a link could be stranded.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from bt_proxy import ble_manager, proto

# Locally-administered addresses (the 0x02 bit is set), so these can never collide with
# a real vendor's device.
ADDRESS = 0x020000000001
OTHER_ADDRESS = 0x020000000002

pytestmark = pytest.mark.asyncio


class Owner:
    """Stand-in for an APIConnection: identity is all the manager uses."""

    def __init__(self, live: bool = True):
        self.live = live


def liveness(*owners: Owner):
    """An owner is live only if it is one of these and still marked live."""

    def _is_live(owner):
        return any(owner is o and o.live for o in owners)

    return _is_live


# --------------------------------------------------------------------------
# A link that arrives after its requester has gone
# --------------------------------------------------------------------------


async def test_connect_that_lands_after_the_requester_gave_up_is_dropped(
    manager, fake_bleak, monkeypatch
):
    """A link established after its requester left must not be kept.

    Two attempts fail. The requester gives up *while the third is in flight* —
    past every cancellation check the loop makes, with the two failures landing
    tens of seconds before the client stops waiting and the successful connect
    arriving after it. The link that arrives must not be kept.
    """
    monkeypatch.setattr(ble_manager, "CONNECT_RETRY_DELAY", 0.0)
    fake_bleak.fail_first = 2
    fake_bleak.gate = asyncio.Event()
    owner = Owner()

    task = asyncio.ensure_future(manager.connect_device(ADDRESS, owner=owner))
    # Wait until the third attempt is committed at the radio, then abandon it.
    await fake_bleak.wait_for_attempt(3)
    await manager.disconnect_device(ADDRESS)
    # Only now let the radio finish, so the connect succeeds with nobody waiting.
    fake_bleak.gate.set()

    success, _mtu, _err = await task

    assert success is False, "an abandoned connect must not report success"
    assert ADDRESS not in manager._connections
    assert manager.free_connections == manager.max_connections
    # The decisive assertion: whatever link the radio established is closed.
    # Left open, it would hold the device off the air for every proxy.
    assert all(
        not c.is_connected for c in fake_bleak.instances
    ), "a link was left open after the requester gave up — the device is now hidden"


async def test_a_dropped_late_connect_leaves_no_untracked_link(
    manager, fake_bleak
):
    """The link must not merely be untracked — it must be closed.

    If the retry loop holds its own reference, a disconnect arriving mid-connect
    removes the manager's record while the loop goes on to establish the link
    anyway — a live connection invisible to free_connections, to the sweeper and
    to cleanup(), held until the process dies. Tracking and reality must agree.
    """
    fake_bleak.gate = asyncio.Event()
    owner = Owner()

    task = asyncio.ensure_future(manager.connect_device(ADDRESS, owner=owner))
    await fake_bleak.wait_for_attempt(1)
    await manager.disconnect_device(ADDRESS)
    fake_bleak.gate.set()
    await task

    connected = [c for c in fake_bleak.instances if c.is_connected]
    tracked = list(manager._connections)
    assert not connected, f"{len(connected)} live link(s) with no record of them"
    assert not tracked


async def test_cancelling_between_retries_stops_the_loop(manager, fake_bleak):
    """The cheaper half: a cancel that lands between attempts stops the retries
    rather than pressing on to establish a link nobody wants."""
    fake_bleak.fail_first = 1
    fake_bleak.connect_delay = 0.02

    task = asyncio.ensure_future(manager.connect_device(ADDRESS, owner=Owner()))
    await fake_bleak.wait_for_attempt(1)
    await manager.disconnect_device(ADDRESS)
    success, _mtu, _err = await task

    assert success is False
    assert fake_bleak.attempts == 1, (
        f"kept retrying after the cancel ({fake_bleak.attempts} attempts)"
    )
    assert all(not c.is_connected for c in fake_bleak.instances)


# --------------------------------------------------------------------------
# The connect budget
# --------------------------------------------------------------------------


async def test_retry_loop_stops_at_the_total_budget(manager, fake_bleak, monkeypatch):
    """The loop must not run past what any caller would wait.

    Unbounded, it runs to CONNECT_MAX_RETRIES * CONNECT_TIMEOUT plus delays —
    ~94s, far longer than any caller waits.
    """
    monkeypatch.setattr(ble_manager, "CONNECT_TOTAL_TIMEOUT", 0.2)
    monkeypatch.setattr(ble_manager, "CONNECT_RETRY_DELAY", 0.05)
    monkeypatch.setattr(ble_manager, "CONNECT_MAX_RETRIES", 10)
    fake_bleak.fail_first = 100  # never succeeds
    fake_bleak.connect_delay = 0.05

    started = time.monotonic()
    success, _mtu, _err = await manager.connect_device(ADDRESS, owner=Owner())
    elapsed = time.monotonic() - started

    assert success is False
    assert elapsed < 1.0, f"loop ran {elapsed:.2f}s against a 0.2s budget"
    assert fake_bleak.attempts < 10, (
        f"made {fake_bleak.attempts} attempts — the budget did not stop the loop"
    )


async def test_a_normal_connect_still_succeeds(manager, fake_bleak):
    """The budget and the ownership checks must not break the ordinary path."""
    owner = Owner()
    success, mtu, err = await manager.connect_device(ADDRESS, owner=owner)

    assert success is True
    assert mtu == 23
    assert err == 0
    assert manager._connections[ADDRESS].connected
    assert manager._owners[ADDRESS] is owner
    assert manager.free_connections == manager.max_connections - 1


async def test_a_first_attempt_failure_still_retries_to_success(
    manager, fake_bleak
):
    """Retrying is the point of the loop; the budget must not neuter it."""
    fake_bleak.fail_first = 1
    success, _mtu, _err = await manager.connect_device(ADDRESS, owner=Owner())

    assert success is True
    assert fake_bleak.attempts == 2


# --------------------------------------------------------------------------
# Ownership: releasing a client's links
# --------------------------------------------------------------------------


async def test_losing_a_client_releases_exactly_its_own_links(
    manager, fake_bleak
):
    """A departing client takes its links with it — and only its own."""
    mine, theirs = Owner(), Owner()
    await manager.connect_device(ADDRESS, owner=mine)
    await manager.connect_device(OTHER_ADDRESS, owner=theirs)

    await manager.release_owner(mine)

    assert ADDRESS not in manager._connections
    assert OTHER_ADDRESS in manager._connections, (
        "released another client's link — the blast radius is too wide"
    )
    assert manager._connections[OTHER_ADDRESS].connected


async def test_release_owner_with_no_links_is_harmless(manager, fake_bleak):
    await manager.connect_device(ADDRESS, owner=Owner())
    await manager.release_owner(Owner())
    assert ADDRESS in manager._connections


# --------------------------------------------------------------------------
# The sweeper. Two halves: what it takes, and what it must spare.
# --------------------------------------------------------------------------


async def test_sweeper_releases_a_link_whose_owner_is_gone(manager, fake_bleak):
    owner = Owner()
    manager.set_owner_liveness(liveness(owner))
    await manager.connect_device(ADDRESS, owner=owner)

    owner.live = False
    released = await manager.sweep_orphans()

    assert released == 1
    assert ADDRESS not in manager._connections


async def test_sweeper_spares_a_link_whose_owner_is_present(manager, fake_bleak):
    owner = Owner()
    manager.set_owner_liveness(liveness(owner))
    await manager.connect_device(ADDRESS, owner=owner)

    assert await manager.sweep_orphans() == 0
    assert manager._connections[ADDRESS].connected


async def test_sweeper_spares_a_silent_link(manager, fake_bleak):
    """Ownership, not idleness, is the axis.

    A link behind a notify subscription can sit silent for hours and be
    perfectly healthy. An idle-timer sweeper would kill it; this one must not
    care how long it has been quiet.
    """
    owner = Owner()
    manager.set_owner_liveness(liveness(owner))
    await manager.connect_device(ADDRESS, owner=owner)

    conn = manager._connections[ADDRESS]
    for _ in range(5):
        assert await manager.sweep_orphans() == 0
    assert conn.connected


async def test_sweeper_does_nothing_when_liveness_is_unknown(
    manager, fake_bleak
):
    """With no way to ask, it must leave everything alone.

    A sweeper that guesses is worse than none: it would disconnect working
    links on every pass.
    """
    await manager.connect_device(ADDRESS, owner=Owner())
    assert manager._owner_is_live is None

    assert await manager.sweep_orphans() == 0
    assert manager._connections[ADDRESS].connected


async def test_sweeper_ignores_a_connect_still_in_flight(manager, fake_bleak):
    """A connect being established is the connect path's business, not the
    sweeper's — otherwise the two race over the same address."""
    owner = Owner()
    manager.set_owner_liveness(liveness(owner))
    fake_bleak.connect_delay = 0.1

    task = asyncio.ensure_future(manager.connect_device(ADDRESS, owner=owner))
    await asyncio.sleep(0.02)
    assert ADDRESS in manager._connecting

    assert await manager.sweep_orphans() == 0
    assert await task == (True, 23, 0)


# --------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------


async def test_cleanup_closes_every_link_and_forgets_its_owners(
    manager, fake_bleak
):
    await manager.connect_device(ADDRESS, owner=Owner())
    await manager.connect_device(OTHER_ADDRESS, owner=Owner())

    await manager.cleanup()

    assert not manager._connections
    assert not manager._owners
    assert all(not c.is_connected for c in fake_bleak.instances)


async def test_mac_round_trips(manager):
    """Guards the address used throughout this file against a silent change."""
    assert proto.int_to_mac(ADDRESS) == "02:00:00:00:00:01"


# --------------------------------------------------------------------------
# The link our own bookkeeping has already let go of
# --------------------------------------------------------------------------


class FakeBlueZ:
    """What BlueZ says is connected, and what it was asked to drop."""

    def __init__(self, manager, monkeypatch):
        self.connected: dict[int, str] | None = {}
        self.dropped: list[str] = []

        async def _connected():
            return None if self.connected is None else dict(self.connected)

        async def _disconnect(path):
            self.dropped.append(path)

        monkeypatch.setattr(manager, "_bluez_connected", _connected)
        monkeypatch.setattr(manager, "_bluez_disconnect", _disconnect)

    def holds(self, address: int) -> None:
        self.connected[address] = f"/org/bluez/hci0/dev_{address:012X}"


@pytest.fixture
def bluez(manager, monkeypatch):
    return FakeBlueZ(manager, monkeypatch)


async def _connect_that_times_out_and_is_forgotten(manager, fake_bleak, monkeypatch):
    monkeypatch.setattr(ble_manager, "CONNECT_RETRY_DELAY", 0.0)
    fake_bleak.fail_first = ble_manager.CONNECT_MAX_RETRIES
    ok, _, _ = await manager.connect_device(ADDRESS, owner=Owner())
    assert not ok and ADDRESS not in manager._connections


async def test_a_link_bluez_completes_after_we_gave_up_is_released(
    manager, fake_bleak, bluez, monkeypatch
):
    """Every attempt fails here and the address is forgotten — then BlueZ, which
    was still trying, completes the connection. The ownership sweep cannot see
    it: there is no entry left to have an owner."""
    await _connect_that_times_out_and_is_forgotten(manager, fake_bleak, monkeypatch)
    bluez.holds(ADDRESS)
    assert await manager.sweep_orphans() == 0
    assert await manager.sweep_untracked_links() == 0, "first sighting only"
    assert await manager.sweep_untracked_links() == 1
    assert bluez.dropped == [f"/org/bluez/hci0/dev_{ADDRESS:012X}"]


async def test_a_link_this_proxy_never_asked_for_is_never_released(manager, bluez):
    """A bonded phone, or a second daemon sharing the adapter."""
    bluez.holds(OTHER_ADDRESS)
    for _ in range(3):
        assert await manager.sweep_untracked_links() == 0
    assert bluez.dropped == []


async def test_a_tracked_link_is_never_released(manager, fake_bleak, bluez):
    owner = Owner()
    manager._owner_is_live = liveness(owner)
    ok, _, _ = await manager.connect_device(ADDRESS, owner=owner)
    assert ok
    bluez.holds(ADDRESS)
    for _ in range(3):
        assert await manager.sweep_untracked_links() == 0
    assert bluez.dropped == []


async def test_a_connect_in_flight_is_never_released(
    manager, fake_bleak, bluez, monkeypatch
):
    await _connect_that_times_out_and_is_forgotten(manager, fake_bleak, monkeypatch)
    bluez.holds(ADDRESS)
    manager._connecting.add(ADDRESS)
    for _ in range(3):
        assert await manager.sweep_untracked_links() == 0
    assert bluez.dropped == []


async def test_a_link_seen_once_and_gone_is_left_alone(
    manager, fake_bleak, bluez, monkeypatch
):
    """BlueZ and the bookkeeping momentarily out of step, e.g. mid-disconnect."""
    await _connect_that_times_out_and_is_forgotten(manager, fake_bleak, monkeypatch)
    bluez.holds(ADDRESS)
    assert await manager.sweep_untracked_links() == 0
    bluez.connected.clear()
    assert await manager.sweep_untracked_links() == 0
    bluez.holds(ADDRESS)
    assert await manager.sweep_untracked_links() == 0, "the count starts again"
    assert bluez.dropped == []


async def test_nothing_is_released_when_bluez_cannot_be_asked(
    manager, fake_bleak, bluez, monkeypatch
):
    await _connect_that_times_out_and_is_forgotten(manager, fake_bleak, monkeypatch)
    bluez.holds(ADDRESS)
    assert await manager.sweep_untracked_links() == 0
    held, bluez.connected = bluez.connected, None
    assert await manager.sweep_untracked_links() == 0
    bluez.connected = held
    assert await manager.sweep_untracked_links() == 0, "an unanswered sweep is not a sighting"
    assert bluez.dropped == []
