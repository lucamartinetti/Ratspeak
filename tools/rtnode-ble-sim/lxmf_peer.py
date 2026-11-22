#!/usr/bin/env python3
"""An LXMF peer on the far side of the simulated RTNode.

It joins the simulator's Reticulum node over TCP, so a phone on the
simulator's Bluetooth side reaches it through one transport hop, the way a
phone reaches someone behind an RTNode. It announces itself, prints every
message it receives as a JSON line, answers each with "echo: <text>", and
sends what `--send` asks for once the destination has announced.

Usage:
    lxmf_peer.py STATE_DIR [--port 4242] [--name Bob]
    lxmf_peer.py STATE_DIR --send <destination hash> "text"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time

import LXMF
import RNS


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


class AnnounceLog:
    """Every LXMF delivery announce heard, with its hop count."""

    aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash, announced_identity, app_data):
        name = LXMF.display_name_from_app_data(app_data) if app_data else None
        emit(
            "announce",
            destination=destination_hash.hex(),
            name=name,
            hops=RNS.Transport.hops_to(destination_hash),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("state_dir")
    parser.add_argument("--port", type=int, default=4242, help="the simulator's TCP port")
    parser.add_argument("--name", default="Bob")
    parser.add_argument("--announce-every", type=float, default=120.0, help="seconds between announces")
    parser.add_argument("--send", nargs=2, metavar=("DESTINATION", "TEXT"), action="append", default=[])
    parser.add_argument("--no-echo", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.state_dir, exist_ok=True)
    with open(os.path.join(args.state_dir, "config"), "w", encoding="utf-8") as out:
        out.write(
            textwrap.dedent(
                f"""\
                [reticulum]
                  enable_transport = No
                  share_instance = No

                [logging]
                  loglevel = 4

                [interfaces]
                  [[RTNode simulator]]
                    type = TCPClientInterface
                    enabled = yes
                    target_host = 127.0.0.1
                    target_port = {args.port}
                """
            )
        )
    RNS.Reticulum(configdir=args.state_dir)

    identity_path = os.path.join(args.state_dir, "lxmf_identity")
    if os.path.isfile(identity_path):
        identity = RNS.Identity.from_file(identity_path)
    else:
        identity = RNS.Identity()
        identity.to_file(identity_path)

    router = LXMF.LXMRouter(storagepath=os.path.join(args.state_dir, "lxmf"))
    destination = router.register_delivery_identity(identity, display_name=args.name)
    RNS.Transport.register_announce_handler(AnnounceLog())
    emit("ready", destination=destination.hash.hex(), name=args.name)

    def send(to: bytes, text: str) -> None:
        recipient_identity = RNS.Identity.recall(to)
        if recipient_identity is None:
            emit("send_failed", destination=to.hex(), reason="identity unknown")
            return
        recipient = RNS.Destination(
            recipient_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery"
        )
        message = LXMF.LXMessage(recipient, destination, text, desired_method=LXMF.LXMessage.DIRECT)
        message.register_delivery_callback(
            lambda m: emit("delivered", destination=to.hex(), text=text)
        )
        message.register_failed_callback(
            lambda m: emit("send_failed", destination=to.hex(), text=text, reason="not delivered")
        )
        router.handle_outbound(message)
        emit("sent", destination=to.hex(), text=text)

    def on_message(message) -> None:
        text = message.content_as_string()
        emit(
            "message",
            source=message.source_hash.hex(),
            text=text,
            title=message.title_as_string(),
            signature_validated=message.signature_validated,
        )
        if not args.no_echo:
            send(message.source_hash, f"echo: {text}")

    router.register_delivery_callback(on_message)

    pending = [(bytes.fromhex(to), text) for to, text in args.send]
    next_announce = 0.0
    while True:
        now = time.monotonic()
        if now >= next_announce:
            router.announce(destination.hash)
            emit("announced", destination=destination.hash.hex())
            next_announce = now + args.announce_every
        for item in list(pending):
            to, text = item
            if RNS.Identity.recall(to) is not None:
                pending.remove(item)
                send(to, text)
            elif not RNS.Transport.has_path(to):
                RNS.Transport.request_path(to)
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
