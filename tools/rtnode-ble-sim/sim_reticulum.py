"""The simulator's Reticulum side: a stock transport node whose interfaces are
the Bluetooth slots, as RTNode's are, and a TCP server that other Reticulum
nodes on this machine join.

The node is the reference Python Reticulum with transport on. RTNode's own
transport rules are not reproduced.
"""

from __future__ import annotations

import os
import textwrap

BRIDGE = None

# The interface module Reticulum loads from <state>/interfaces. Reticulum
# executes it with `Interface` and `RNS` already defined.
SLOT_INTERFACE = '''
import sim_reticulum


class RTNodeSimSlot(Interface):
    """One Bluetooth slot. RTNode's BlePeerInterface: 500-byte packets, a fixed
    MTU, the bitrate ble-reticulum guesses, online while a peer is identified."""

    DEFAULT_IFAC_SIZE = 8

    def __init__(self, owner, configuration):
        super().__init__()
        config = Interface.get_config_obj(configuration)
        self.owner = owner
        self.name = config["name"]
        self.slot = int(config["slot"])
        self.HW_MTU = 500
        self.FIXED_MTU = True
        self.bitrate = 700000
        self.IN = True
        self.OUT = True
        self.online = False
        sim_reticulum.BRIDGE.interfaces[self.slot] = self

    def process_incoming(self, data):
        self.rxb += len(data)
        self.owner.inbound(data, self)

    def process_outgoing(self, data):
        if self.online and sim_reticulum.BRIDGE.send(self.slot, bytes(data)) is not False:
            self.txb += len(data)

    def __str__(self):
        return f"RTNodeSimSlot[{self.name}]"


interface_class = RTNodeSimSlot
'''


class Bridge:
    def __init__(self, slots: int):
        self.interfaces = [None] * slots
        self.identity = b""
        # Set by the peripheral: send(slot, packet).
        self.send = lambda index, packet: False

    def deliver(self, index: int, packet: bytes) -> None:
        interface = self.interfaces[index]
        if interface is not None and interface.online:
            interface.process_incoming(packet)

    def set_online(self, index: int, online: bool) -> None:
        interface = self.interfaces[index]
        if interface is not None:
            interface.online = online


def start(state_dir: str, slots: int, tcp_port: int, loglevel: int = 4) -> Bridge:
    """Start Reticulum with its state in `state_dir`."""
    global BRIDGE  # pylint: disable=global-statement
    import RNS  # pylint: disable=import-outside-toplevel

    os.makedirs(os.path.join(state_dir, "interfaces"), exist_ok=True)
    with open(os.path.join(state_dir, "interfaces", "RTNodeSimSlot.py"), "w", encoding="utf-8") as out:
        out.write(SLOT_INTERFACE)
    slot_sections = "".join(
        f"  [[BLE{index + 1}]]\n    type = RTNodeSimSlot\n    enabled = yes\n    slot = {index}\n\n"
        for index in range(slots)
    )
    config = textwrap.dedent(
        """\
        [reticulum]
          enable_transport = Yes
          share_instance = No
          panic_on_interface_error = No

        [logging]
          loglevel = LOGLEVEL

        [interfaces]
        """
    ).replace("LOGLEVEL", str(loglevel)) + slot_sections + textwrap.dedent(
        f"""\
          [[Backbone]]
            type = TCPServerInterface
            enabled = yes
            listen_ip = 127.0.0.1
            listen_port = {tcp_port}
            # Test peers announce far more often than once an hour, which
            # Reticulum would otherwise stop rebroadcasting.
            announce_rate_target = 0
        """
    )
    with open(os.path.join(state_dir, "config"), "w", encoding="utf-8") as out:
        out.write(config)

    BRIDGE = Bridge(slots)
    RNS.Reticulum(configdir=state_dir)
    BRIDGE.identity = RNS.Transport.identity.hash
    missing = [index for index, interface in enumerate(BRIDGE.interfaces) if interface is None]
    if missing:
        raise RuntimeError(f"Reticulum did not create the interfaces of slots {missing}")
    RNS.log(f"Transport identity {RNS.prettyhexrep(BRIDGE.identity)}", RNS.LOG_NOTICE)
    return BRIDGE
