# RTNode Bluetooth simulator

A Bluetooth LE peripheral that behaves like an
[RTNode](https://github.com/jrl290/RTNode-HeltecV4)'s, on a virtual radio, for
testing Bluetooth Peer's Central role without the hardware. It was written to
test Ratspeak in the Android emulator, whose Bluetooth is virtual too.

- `rtnode_ble_sim.py` is the peripheral: RTNode's GATT table, advertisement
  and handling of everything a central writes, following `BleInterface.h` and
  `BleColumba.h` at RTNode commit `82ae046`. Each function names the firmware
  function it follows.
- `sim_reticulum.py` makes the peripheral's four peer slots interfaces of a
  Reticulum transport node in the same process, as RTNode's are, and opens a
  TCP port for other Reticulum nodes on this machine.
- `lxmf_peer.py` is an LXMF peer behind that TCP port: someone to message
  through the node. It echoes what it receives.

It does not reproduce NimBLE's queues, RTNode's own transport rules (its
firewall and route choice; the node here is stock Python Reticulum), or the
Prns protocol beyond the presence of its two characteristics.

## Setup

```sh
python3 -m venv ~/.cache/rtnode-ble-sim
~/.cache/rtnode-ble-sim/bin/pip install bumble grpcio protobuf rns lxmf
```

## Run against the Android emulator

Start an emulator (API 33 or later, so that its Bluetooth goes through netsim),
then:

```sh
cd tools/rtnode-ble-sim
PY=~/.cache/rtnode-ble-sim/bin/python
$PY rtnode_ble_sim.py android-netsim --reticulum /tmp/rtnode-sim/node --trace
$PY lxmf_peer.py /tmp/rtnode-sim/bob          # in a second terminal
```

In Ratspeak on the emulator, turn on Network › Bluetooth Peer. The simulator
logs the connection the way RTNode's serial console does:

```
[BLE] BLE1: connected: 73:4C:0E:43:24:74
[BLE] BLE1: Columba peer c32dbb30 identified, MTU 517
[BLE] BLE1 <- ANNOUNCE to 3ffd336e, hops 0, context 0x00, 238 bytes, 1 fragments
```

Network shows a Reticulum node row, Peers lists Bob once his announce comes
through, and a message to Bob comes back as `echo: …`. A central that does
not speak the protocol shows as `… bytes before the identity, dropped`.

Options worth knowing:

- `--max-mtu 64` agrees to a small ATT MTU, so the central has to fragment.
- `--fragment-data-max 40` sends small fragments, like the firmware's
  `RTNODE_BLE_CHUNK_MAX`, to exercise the central's reassembly.
- `--report FILE` writes the counters RTNode prints every minute to a JSON
  file every 2 seconds.
- Any other [Bumble transport](https://google.github.io/bumble/transports/)
  works in place of `android-netsim`, for example `usb:0` for a spare
  Bluetooth dongle.

The far peer announces every two minutes; Reticulum stops rebroadcasting a
destination that announces much more often, which the simulator's TCP port is
configured to allow.

## License

`rtnode_ble_sim.py` is under GPL-3.0, not Ratspeak's license: it follows
RTNode's `BleInterface.h`, which is GPL-3.0, function by function. Its header
has the details. `sim_reticulum.py` and `lxmf_peer.py` are under Ratspeak's
license. No Ratspeak build includes any of the three.
