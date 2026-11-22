#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# This file is under the GNU General Public License v3.0
# (https://www.gnu.org/licenses/gpl-3.0.txt), not Ratspeak's license: it
# follows BleInterface.h of RTNode-HeltecV4
# (https://github.com/jrl290/RTNode-HeltecV4, commit 82ae046, GPL-3.0)
# function by function, and BleColumba.h of the same repository (MIT,
# Copyright (c) 2025 torlando-tech, Copyright (c) 2026 James LaFarge). It is
# a test tool that runs on its own; no part of Ratspeak includes it.
"""A Bluetooth LE peripheral that behaves like RTNode's, for testing a central.

RTNode (https://github.com/jrl290/RTNode-HeltecV4) lets phones join it over
Bluetooth LE with the ble-reticulum protocol. This program is that peripheral
on a virtual controller: the GATT table, the advertisement, and the handling
of everything a central writes follow RTNode's BleInterface.h and BleColumba.h
at commit 82ae046. Each function below names the firmware function it
follows. With --reticulum the peers are also interfaces of a Reticulum
transport node in this process (sim_reticulum.py), as RTNode's slots are.

What it does not reproduce: NimBLE's queues and backpressure, RTNode's own
transport rules (its firewall, its route choice), and the Prns protocol
beyond the presence of its two characteristics.

Usage:
    rtnode_ble_sim.py android-netsim            # the Android emulator's radio
    rtnode_ble_sim.py android-netsim --reticulum STATE_DIR --tcp-port 4242
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field

from bumble.core import UUID, AdvertisingData
from bumble.device import Connection, Device
from bumble.gatt import Characteristic, CharacteristicValue, Service
from bumble.hci import Address
from bumble.transport import open_transport

log = logging.getLogger("rtnode-sim")

# ── BleColumba.h ─────────────────────────────────────────────────────────────
SERVICE_UUID = "37145b00-442d-4a94-917f-8f42c5da28e3"
TX_UUID = "37145b00-442d-4a94-917f-8f42c5da28e4"
RX_UUID = "37145b00-442d-4a94-917f-8f42c5da28e5"
IDENTITY_UUID = "37145b00-442d-4a94-917f-8f42c5da28e6"
IDENTITY_LEN = 16
KEEPALIVE = b"\x00"
KEEPALIVE_EVERY_S = 15.0
# Company 0xFFFF, version 3, peripheral-only.
ADV_MANUFACTURER_DATA = bytes([0xFF, 0xFF, 0x03, 0x01])
FRAG_START, FRAG_CONTINUE, FRAG_END = 0x01, 0x02, 0x03
FRAG_HEADER = 5

# ── BlePrns.h (the characteristics only) ─────────────────────────────────────
PRNS_CONTROL_UUID = "37145b00-442d-4a94-917f-8f42c5da28e7"
PRNS_DATA_UUID = "37145b00-442d-4a94-917f-8f42c5da28e8"

# ── BleInterface.h ───────────────────────────────────────────────────────────
VALUE_MAX = 512
PACKET_MAX = 512
SLOTS = 4  # CONFIG_BT_NIMBLE_MAX_CONNECTIONS in RTNode's V4 build
TX_DEPTH = 96
ADV_INTERVAL_MIN_MS = 100
ADV_INTERVAL_MAX_MS = 150


def is_keepalive(data: bytes) -> bool:
    """columba::is_keepalive"""
    return len(data) == 1 and data[0] == KEEPALIVE[0]


def read_fragment(data: bytes) -> tuple[int, int, int] | None:
    """columba::read_fragment: (type, sequence, total), or None."""
    if len(data) < FRAG_HEADER:
        return None
    kind = data[0]
    seq = (data[1] << 8) | data[2]
    total = (data[3] << 8) | data[4]
    if FRAG_START <= kind <= FRAG_END and total != 0 and seq < total:
        return kind, seq, total
    return None


def fragment_header(seq: int, total: int) -> bytes:
    """columba::write_fragment_header"""
    kind = FRAG_START if seq == 0 else (FRAG_END if seq == total - 1 else FRAG_CONTINUE)
    return bytes([kind, seq >> 8, seq & 0xFF, total >> 8, total & 0xFF])


def short_identity(identity: bytes) -> str:
    return identity[:4].hex()


def describe_packet(packet: bytes) -> str:
    """A Reticulum packet's header, for --trace."""
    if len(packet) < 19:
        return f"{len(packet)} bytes, too short for a Reticulum packet ({packet.hex()})"
    flags, hops = packet[0], packet[1]
    header_2 = bool(flags & 0x40)
    kind = ("DATA", "ANNOUNCE", "LINKREQUEST", "PROOF")[flags & 0x03]
    destination = packet[18:34] if header_2 else packet[2:18]
    context = packet[34] if header_2 and len(packet) > 34 else packet[18]
    via = f" via {packet[2:6].hex()}" if header_2 else ""
    return (
        f"{kind} to {destination[:4].hex()}{via}, hops {hops}, context 0x{context:02x}, "
        f"{len(packet)} bytes"
    )


@dataclass
class Slot:
    """One peer: BlePeerInterface's state."""

    name: str
    used: bool = False
    identified: bool = False
    connection: Connection | None = None
    identity: bytes = b""
    connected_at: float = 0.0
    last_sent: float = 0.0
    rx: bytearray = field(default_factory=bytearray)
    rx_total: int = 0
    rx_next: int = 0
    rx_active: bool = False
    tx: asyncio.Queue | None = None
    sender: asyncio.Task | None = None
    # Counters since start, as RTNode's report() prints them.
    rx_packets: int = 0
    rx_bytes: int = 0
    tx_packets: int = 0
    tx_bytes: int = 0
    tx_drops: int = 0
    bad_fragments: int = 0
    keepalives: int = 0
    keepalives_sent: int = 0
    before_identity: int = 0
    notify_errors: int = 0
    peers: int = 0

    def counters(self) -> dict:
        return {
            "slot": self.name,
            "state": "up" if self.identified else ("joining" if self.used else "free"),
            "peer": short_identity(self.identity) if self.identified else "-",
            "mtu": self.connection.att_mtu if self.connection else 0,
            "peers": self.peers,
            "rx_packets": self.rx_packets,
            "rx_bytes": self.rx_bytes,
            "tx_packets": self.tx_packets,
            "tx_bytes": self.tx_bytes,
            "tx_drops": self.tx_drops,
            "bad_fragments": self.bad_fragments,
            "keepalives_in": self.keepalives,
            "keepalives_out": self.keepalives_sent,
            "before_identity": self.before_identity,
            "notify_errors": self.notify_errors,
        }


class RTNodeBle:
    """The peripheral. `on_packet(slot_index, packet)` receives each packet a
    peer sends; `on_peer(slot_index, online)` follows identification."""

    def __init__(
        self,
        device: Device,
        identity: bytes,
        fragment_data_max: int | None = None,
        max_mtu: int = 517,
        trace: bool = False,
    ):
        self.device = device
        self.identity = identity
        self.trace = trace
        # RTNODE_BLE_CHUNK_MAX: small fragments, to exercise a peer's reassembly.
        self.fragment_data_max = fragment_data_max
        self.slots = [Slot(f"BLE{i + 1}") for i in range(SLOTS)]
        self.on_packet = lambda index, packet: None
        self.on_peer = lambda index, online: None
        self.orphan_writes = 0
        self.self_connections = 0
        self.replaced = 0

        self.tx_char = Characteristic(
            TX_UUID,
            Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            b"",
        )
        rx_char = Characteristic(
            RX_UUID,
            Characteristic.Properties.WRITE | Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=self.on_rx_write),
        )
        id_char = Characteristic(
            IDENTITY_UUID,
            Characteristic.Properties.READ,
            Characteristic.READABLE,
            identity,
        )
        control_char = Characteristic(
            PRNS_CONTROL_UUID,
            Characteristic.Properties.WRITE | Characteristic.Properties.NOTIFY,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=self.on_prns_write),
        )
        data_char = Characteristic(
            PRNS_DATA_UUID,
            Characteristic.Properties.WRITE
            | Characteristic.Properties.WRITE_WITHOUT_RESPONSE
            | Characteristic.Properties.NOTIFY,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=self.on_prns_write),
        )
        device.add_service(
            Service(SERVICE_UUID, [self.tx_char, rx_char, id_char, control_char, data_char])
        )
        # NimBLEDevice::setMTU(517). A smaller one makes the central fragment.
        device.gatt_server.max_mtu = max_mtu
        device.on("connection", self.on_connect)

    # ── start() ──────────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self.device.power_on()
        await self.advertise()
        log.info(
            "[BLE] up: %d slots, identity %s, address %s (random)",
            SLOTS,
            short_identity(self.identity),
            self.device.random_address,
        )

    async def advertise(self) -> None:
        data = AdvertisingData(
            [
                # BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP
                (AdvertisingData.FLAGS, bytes([0x06])),
                (
                    AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
                    bytes(UUID(SERVICE_UUID)),
                ),
                (AdvertisingData.MANUFACTURER_SPECIFIC_DATA, ADV_MANUFACTURER_DATA),
            ]
        )
        try:
            await self.device.start_advertising(
                advertising_data=bytes(data),
                scan_response_data=b"",
                advertising_interval_min=ADV_INTERVAL_MIN_MS,
                advertising_interval_max=ADV_INTERVAL_MAX_MS,
            )
        except Exception as error:  # pylint: disable=broad-except
            log.warning("[BLE] advertising not started: %s", error)

    def slot_for(self, connection: Connection) -> tuple[int, Slot] | tuple[None, None]:
        for index, slot in enumerate(self.slots):
            if slot.used and slot.connection is connection:
                return index, slot
        return None, None

    # ── on_connect / on_disconnect ───────────────────────────────────────────
    def on_connect(self, connection: Connection) -> None:
        for slot in self.slots:
            if not slot.used:
                # BlePeerInterface::attach
                slot.used = True
                slot.identified = False
                slot.connection = connection
                slot.connected_at = time.monotonic()
                slot.rx_active = False
                slot.tx = asyncio.Queue(maxsize=TX_DEPTH)
                log.info("[BLE] %s: connected: %s", slot.name, connection.peer_address)
                break
        else:
            log.info("[BLE] no free slot for %s", connection.peer_address)
        connection.on("disconnection", lambda reason: self.on_disconnect(connection, reason))
        connection.on(
            "connection_att_mtu_update",
            lambda: log.info("[BLE] %s: MTU %d", connection.peer_address, connection.att_mtu),
        )
        # Advertising stops on connect; keep it up while a slot is free.
        if sum(slot.used for slot in self.slots) < SLOTS:
            asyncio.get_running_loop().create_task(self.advertise())

    def on_disconnect(self, connection: Connection, reason: int) -> None:
        index, slot = self.slot_for(connection)
        if slot is None:
            return
        if slot.identified:
            log.info(
                "[BLE] %s: peer %s left (reason 0x%x) after %ds",
                slot.name,
                short_identity(slot.identity),
                reason,
                time.monotonic() - slot.connected_at,
            )
            self.on_peer(index, False)
        # BlePeerInterface::release
        if slot.sender:
            slot.sender.cancel()
        slot.used = False
        slot.identified = False
        slot.connection = None
        slot.rx_active = False
        slot.tx = None
        slot.sender = None
        asyncio.get_running_loop().create_task(self.advertise())

    # ── on_identity ──────────────────────────────────────────────────────────
    def on_identity(self, index: int, identity: bytes) -> None:
        slot = self.slots[index]
        if identity == self.identity:
            self.self_connections += 1
            self.disconnect(slot)
            return
        # The same peer on another slot: it reconnected and the old link is
        # gone from its side. Keep the new one.
        for other_index, other in enumerate(self.slots):
            if other_index != index and other.used and other.identified and other.identity == identity:
                self.replaced += 1
                log.info("[BLE] %s: replaced by %s", other.name, slot.name)
                self.disconnect(other)
        slot.identity = identity
        # BlePeerInterface::set_online
        slot.identified = True
        slot.last_sent = time.monotonic()
        slot.peers += 1
        log.info(
            "[BLE] %s: Columba peer %s identified, MTU %d",
            slot.name,
            short_identity(identity),
            slot.connection.att_mtu,
        )
        slot.sender = asyncio.get_running_loop().create_task(self.send_loop(slot))
        asyncio.get_running_loop().create_task(self.request_fast_link(slot))
        self.on_peer(index, True)

    def disconnect(self, slot: Slot) -> None:
        if slot.connection is not None:
            asyncio.get_running_loop().create_task(slot.connection.disconnect())

    async def request_fast_link(self, slot: Slot) -> None:
        """request_fast_link: 251-byte link-layer packets, the 2M PHY and a
        15 ms interval. The phone decides; a refusal changes nothing here."""
        connection = slot.connection
        if connection is None:
            return
        for label, request in (
            ("251-byte link-layer packets", lambda: connection.set_data_length(251, 2120)),
            ("a 15 ms interval", lambda: connection.update_parameters(15, 15, 0, 4000)),
        ):
            try:
                await asyncio.wait_for(request(), timeout=5)
                log.info("[BLE] %s: asked for %s", slot.name, label)
            except Exception as error:  # pylint: disable=broad-except
                log.info("[BLE] %s: asked for %s: %s", slot.name, label, error or type(error).__name__)

    # ── on_write ─────────────────────────────────────────────────────────────
    def on_prns_write(self, connection: Connection, value: bytes) -> None:
        log.info("[BLE] write to a Prns characteristic from %s, ignored", connection.peer_address)

    def on_rx_write(self, connection: Connection, value: bytes) -> None:
        data = bytes(value)[:VALUE_MAX]
        index, slot = self.slot_for(connection)
        if slot is None:
            self.orphan_writes += 1
            return
        if is_keepalive(data):
            slot.keepalives += 1
            return
        if not slot.identified:
            if len(data) == IDENTITY_LEN:
                self.on_identity(index, data)
            else:
                slot.before_identity += 1
                log.info(
                    "[BLE] %s: %d bytes before the identity, dropped (%s)",
                    slot.name,
                    len(data),
                    data[:8].hex(),
                )
            return
        fragment = read_fragment(data)
        if fragment is None:
            slot.bad_fragments += 1
            slot.rx_active = False
            log.info("[BLE] %s: bad fragment, %d bytes (%s)", slot.name, len(data), data[:8].hex())
            return
        _, seq, total = fragment
        payload = data[FRAG_HEADER:]
        if seq == 0:
            slot.rx = bytearray()
            slot.rx_total = total
            slot.rx_next = 0
            slot.rx_active = True
        elif not slot.rx_active or total != slot.rx_total or seq != slot.rx_next:
            slot.bad_fragments += 1
            slot.rx_active = False
            log.info("[BLE] %s: fragment %d/%d out of sequence", slot.name, seq, total)
            return
        if len(slot.rx) + len(payload) > PACKET_MAX:
            slot.bad_fragments += 1
            slot.rx_active = False
            return
        slot.rx += payload
        slot.rx_next += 1
        if slot.rx_next == slot.rx_total:
            slot.rx_active = False
            # BlePeerInterface::deliver
            slot.rx_packets += 1
            slot.rx_bytes += len(slot.rx)
            if self.trace:
                log.info("[BLE] %s <- %s, %d fragments", slot.name, describe_packet(bytes(slot.rx)), total)
            self.on_packet(index, bytes(slot.rx))

    # ── send_outgoing / send_next_fragment / the keepalive in loop() ─────────
    def send(self, index: int, packet: bytes) -> bool:
        slot = self.slots[index]
        if not slot.identified or slot.tx is None:
            return False
        if slot.tx.full() or len(packet) > PACKET_MAX:
            slot.tx_drops += 1
            return False
        slot.tx.put_nowait(packet)
        return True

    async def send_loop(self, slot: Slot) -> None:
        connection = slot.connection
        while slot.identified and slot.connection is connection:
            idle = KEEPALIVE_EVERY_S - (time.monotonic() - slot.last_sent)
            try:
                packet = await asyncio.wait_for(slot.tx.get(), timeout=max(idle, 0.01))
            except asyncio.TimeoutError:
                if await self.notify(slot, KEEPALIVE):
                    slot.keepalives_sent += 1
                slot.last_sent = time.monotonic()
                continue
            usable = connection.att_mtu - 3 if connection.att_mtu > 3 else 20
            usable = min(usable, VALUE_MAX)
            if self.fragment_data_max is not None:
                usable = min(usable, self.fragment_data_max + FRAG_HEADER)
            chunk = usable - FRAG_HEADER
            total = (len(packet) + chunk - 1) // chunk
            for seq in range(total):
                data = packet[seq * chunk : (seq + 1) * chunk]
                if not await self.notify(slot, fragment_header(seq, total) + data):
                    # The link is going or gone; this packet will not arrive whole.
                    slot.notify_errors += 1
                    break
                slot.last_sent = time.monotonic()
            else:
                slot.tx_packets += 1
                slot.tx_bytes += len(packet)
                if self.trace:
                    log.info("[BLE] %s -> %s, %d fragments", slot.name, describe_packet(packet), total)

    async def notify(self, slot: Slot, value: bytes) -> bool:
        """ble_gattc_notify_custom: sent whether or not the peer subscribed."""
        try:
            await self.device.notify_subscriber(slot.connection, self.tx_char, value, force=True)
            return True
        except Exception as error:  # pylint: disable=broad-except
            log.info("[BLE] %s: notification failed: %s", slot.name, error)
            return False

    def report(self) -> dict:
        return {
            "orphan_writes": self.orphan_writes,
            "self_connections": self.self_connections,
            "replaced": self.replaced,
            "slots": [slot.counters() for slot in self.slots],
        }


def static_random_address(seed: bytes) -> Address:
    """A static random address fixed per device: a hash with its top two bits
    set, as RTNode derives its own from the chip's MAC."""
    digest = bytearray(hashlib.sha256(seed + b"rtnode-ble").digest()[:6])
    digest[0] |= 0xC0
    return Address(":".join(f"{byte:02X}" for byte in digest), Address.RANDOM_DEVICE_ADDRESS)


async def run(args: argparse.Namespace) -> None:
    bridge = None
    if args.reticulum:
        import sim_reticulum  # pylint: disable=import-outside-toplevel

        bridge = sim_reticulum.start(args.reticulum, SLOTS, args.tcp_port, args.reticulum_loglevel)
        identity = bridge.identity
    else:
        identity = hashlib.sha256(args.name.encode()).digest()[:IDENTITY_LEN]

    log.info("opening %s", args.transport)
    async with await open_transport(args.transport) as transport:
        device = Device.with_hci(
            "", static_random_address(args.name.encode()), transport.source, transport.sink
        )
        node = RTNodeBle(device, identity, args.fragment_data_max, args.max_mtu, args.trace)
        loop = asyncio.get_running_loop()

        if bridge is not None:
            node.on_packet = bridge.deliver
            node.on_peer = bridge.set_online
            # Reticulum sends from its own threads.
            bridge.send = lambda index, packet: loop.call_soon_threadsafe(node.send, index, packet)
        else:
            node.on_packet = lambda index, packet: log.info(
                "[BLE] %s: packet, %d bytes (%s)",
                node.slots[index].name,
                len(packet),
                packet[:16].hex(),
            )

        await node.start()
        report_path = args.report
        while True:
            await asyncio.sleep(2)
            if report_path:
                tmp = report_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as out:
                    json.dump(node.report(), out, indent=1)
                os.replace(tmp, report_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("transport", help="Bumble transport, e.g. android-netsim, vhci, usb:0")
    parser.add_argument("--name", default="rtnode-sim", help="seeds the address (and the identity without --reticulum)")
    parser.add_argument("--reticulum", metavar="DIR", help="run a Reticulum transport node whose state lives in DIR")
    parser.add_argument("--tcp-port", type=int, default=4242, help="TCP port other Reticulum nodes join (with --reticulum)")
    parser.add_argument("--reticulum-loglevel", type=int, default=4, help="Reticulum's log level, 0 to 7")
    parser.add_argument("--fragment-data-max", type=int, help="largest fragment payload sent, like RTNODE_BLE_CHUNK_MAX")
    parser.add_argument("--max-mtu", type=int, default=517, help="largest ATT MTU agreed to; RTNode's is 517")
    parser.add_argument("--report", metavar="FILE", help="write the counters here every 2 s, as JSON")
    parser.add_argument("--trace", action="store_true", help="log every packet in either direction")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if not args.verbose:
        logging.getLogger("bumble").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
