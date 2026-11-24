#!/usr/bin/env python3
"""Traffic test for the leaf-spine-otg topology (Ixia-c via snappi).

Runs on the VM (the Ixia-c service IPs are only reachable there):

    python3 -m venv ~/otg-venv && ~/otg-venv/bin/pip install snappi
    ~/otg-venv/bin/python scripts/otg_test.py

Tester layout (matches the switch config applied on leaf1/spine1/leaf2):

    h1 (port p1, 10.10.1.2/24, gw 10.10.1.1) -- leaf1 -- spine1 -- leaf2 -- h2 (port p2, 10.10.2.2/24, gw 10.10.2.1)

Two flows: h1 -> h2 and h2 -> h1, each 1000 packets at 100 pps.
"""

import sys
import time

import snappi

CONTROLLER = "https://172.18.0.50:8443"
# The controller's location map is keyed by the topology interface name
# (see its "LocationMap" log: eth1 -> otg-port-eth1 pod, eth2 -> otg-port-eth2 pod).
# The controller resolves these to the port pods itself.
PORT_P1 = "eth1"  # leaf1 side  (otg-port-eth1)
PORT_P2 = "eth2"  # leaf2 side  (otg-port-eth2)

H1_IP = "10.10.1.2"
H2_IP = "10.10.2.2"

PACKETS = 1000
PPS = 100
FRAME_SIZE = 128
RUN_SECONDS = PACKETS / PPS + 3


def build_config():
    api = snappi.api(location=CONTROLLER)
    cfg = api.config()

    p1 = cfg.ports.port(name="p1", location=PORT_P1)[-1]
    p2 = cfg.ports.port(name="p2", location=PORT_P2)[-1]

    h1 = cfg.devices.device(name="h1")[-1]
    h1_eth = h1.ethernets.ethernet(name="h1_eth")[-1]
    h1_eth.connection.port_name = p1.name
    h1_eth.mac = "02:00:00:00:01:02"
    h1_ip = h1_eth.ipv4_addresses.ipv4(name="h1_ip")[-1]
    h1_ip.address = "10.10.1.2"
    h1_ip.gateway = "10.10.1.1"
    h1_ip.prefix = 24

    h2 = cfg.devices.device(name="h2")[-1]
    h2_eth = h2.ethernets.ethernet(name="h2_eth")[-1]
    h2_eth.connection.port_name = p2.name
    h2_eth.mac = "02:00:00:00:02:02"
    h2_ip = h2_eth.ipv4_addresses.ipv4(name="h2_ip")[-1]
    h2_ip.address = "10.10.2.2"
    h2_ip.gateway = "10.10.2.1"
    h2_ip.prefix = 24

    flows = (
        ("h1_to_h2", h1_ip, h2_ip, H1_IP, H2_IP),
        ("h2_to_h1", h2_ip, h1_ip, H2_IP, H1_IP),
    )
    for name, src_dev, dst_dev, src_ip, dst_ip in flows:
        flow = cfg.flows.flow(name=name)[-1]
        flow.tx_rx.choice = "device"
        flow.tx_rx.device.tx_names = [src_dev.name]
        flow.tx_rx.device.rx_names = [dst_dev.name]
        flow.size.fixed = FRAME_SIZE
        flow.rate.pps = PPS
        flow.duration.choice = "fixed_packets"
        flow.duration.fixed_packets.packets = PACKETS
        flow.metrics.enable = True

        eth_hdr = flow.packet.add()
        eth_hdr.choice = "ethernet"

        ip_hdr = flow.packet.add()
        ip_hdr.choice = "ipv4"
        ip_hdr.ipv4.src.value = src_ip
        ip_hdr.ipv4.dst.value = dst_ip

        udp_hdr = flow.packet.add()
        udp_hdr.choice = "udp"
        udp_hdr.udp.src_port.value = 5000
        udp_hdr.udp.dst_port.value = 6000

    return api, cfg


def main():
    api, cfg = build_config()

    print("Pushing config to controller ...")
    api.set_config(cfg)

    cs = api.control_state()
    cs.choice = cs.TRAFFIC
    cs.traffic.flow_transmit.state = cs.traffic.flow_transmit.START
    print(f"Starting traffic: {PACKETS} packets x 2 flows at {PPS} pps ...")
    api.set_control_state(cs)

    time.sleep(RUN_SECONDS)

    req = api.metrics_request()
    req.flow.flow_names = ["h1_to_h2", "h2_to_h1"]
    flows = api.get_metrics(req).flow_metrics
    print("\n=== Flow metrics (tester view) ===")
    print(f"{'flow':<12}{'tx_frames':>12}{'rx_frames':>12}{'lost':>8}")
    for fm in flows:
        lost = fm.frames_tx - fm.frames_rx
        print(f"{fm.name:<12}{fm.frames_tx:>12}{fm.frames_rx:>12}{lost:>8}")

    preq = api.metrics_request()
    preq.port.port_names = ["p1", "p2"]
    print("\n=== Port metrics (tester view) ===")
    print(f"{'port':<6}{'tx_frames':>12}{'rx_frames':>12}")
    for pm in api.get_metrics(preq).port_metrics:
        print(f"{pm.name:<6}{pm.frames_tx:>12}{pm.frames_rx:>12}")

    cs.traffic.flow_transmit.state = cs.traffic.flow_transmit.STOP
    api.set_control_state(cs)
    print("\nTraffic stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
