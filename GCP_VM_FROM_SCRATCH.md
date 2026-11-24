# From a blank GCP project to a routed SONiC leaf-spine lab with Ixia-c traffic

This is the full path we followed on one GCP VM: create the VM, build a KNE
(Kubernetes Network Emulation) cluster on it, boot three SONiC virtual switches
as pods, cable them to an Ixia-c traffic generator, configure L3 on the
switches, and send traffic through the fabric.

It stops at **static routes + a passing traffic test**. BGP is not covered
(see [What is not covered](#17-what-is-not-covered)).

> **How much of this was verified?** Every command and result below comes from
> a real run on one VM, except the host-tool install commands in steps 3 and 5
> and the troubleshooting rows marked †. The install commands were run earlier
> by hand, and the versions in this document were read back from the VM
> afterwards. They are the standard install methods, but this document has
> **not** been replayed on a blank VM start to finish.

---

## 0. What you end up with

```
                       +-----------+
                       |  spine1   |
                       +--+-----+--+
                  eth1 /         \ eth2
                      /           \
                eth2 /             \ eth2
              +-----+---+       +---+-----+
              |  leaf1  |       |  leaf2  |
              +----+----+       +----+----+
               eth1 |                | eth1
                    |                |
              +-----+----------------+-----+
              |   Ixia-c  port 1   port 2  |
              +----------------------------+
```

- One `n2-standard-32` VM, nested virtualization on.
- One single-node `kind` cluster (`kne`) on that VM, with MetalLB, meshnet and
  the KNE vendor controllers.
- Three switch pods. Each pod runs QEMU/KVM with a SONiC virtual-switch disk.
- One Ixia-c "node" that KNE expands into a controller pod and two port pods.
- Static L3 routes on the switches, and a Python (snappi) test that sends
  1,000 packets each way and reads the counters.

Why a VM and Kubernetes at all: each switch is a *virtual machine* inside a
container, so the host needs `/dev/kvm`; and KNE gives you a text file that
describes the cabling, with Kubernetes doing the scheduling and lifecycle.

---

## 1. Prerequisites

You need these before you start. The last two are the parts this repo cannot
provide.

- A GCP project with quota for 32 vCPUs of `N2` and a 512 GB disk.
- `gcloud` installed and logged in on your laptop.
- A **SONiC virtual-switch disk image** (qcow2) that boots under QEMU with KVM.
- A **container image** that wraps that disk and has this contract:
  - it starts with `/startup.sh`,
  - it reads `SWITCH_ID`, `TOPO_ID` and `MGMT_PREFIX` from the environment,
  - it connects the pod's `eth1`, `eth2`, ... to the VM's data ports,
  - it gives the switch a management IP of `<MGMT_PREFIX>.<TOPO_ID>.<SWITCH_ID>`.

  In this document that image is called `YOUR_SWITCH_IMAGE:latest`. The topology
  in this repo works only with an image that follows that contract.

The environment this was verified on:

| Item | Value |
|---|---|
| VM | `n2-standard-32`, 32 vCPU, 125 GB RAM, 512 GB `pd-standard` |
| OS | Ubuntu 26.04 LTS, kernel `7.0.0-1017-gcp` |
| Docker | 29.1.3 (Ubuntu `docker.io` package) |
| Go | 1.25.4 |
| kind | 0.27.0 (node image `kindest/node:v1.26.0`) |
| kubectl | v1.37.2 |
| KNE | `openconfig/kne` main at commit `68d11a4` (Nov 20) |
| snappi | 1.65.0 (Python 3.14.4) |
| Ixia-c | controller `1.61.0-1`, traffic engine `1.8.0.544`, protocol engine `1.00.0.534` |

---

## 2. Create the VM

The one setting to get right at creation is **nested virtualization**. A VM
created without it does not get `/dev/kvm` by adding a user to the `kvm` group.
In our case we recreated the VM with the flag, on an Intel `N2` machine type.
`E2`, `N2D` (AMD) and Arm types do not expose `vmx`.

```bash
gcloud compute instances create sonicvs-kne \
  --project YOUR_PROJECT \
  --zone us-central1-f \
  --machine-type n2-standard-32 \
  --min-cpu-platform "Intel Cascade Lake" \
  --enable-nested-virtualization \
  --image-family ubuntu-2604-lts-amd64 \
  --image-project ubuntu-os-cloud \
  --boot-disk-size 512GB \
  --boot-disk-type pd-standard \
  --scopes cloud-platform
```

`pd-standard` is a hard disk: building the image and the first boot of each
switch are slow on it. Use `pd-ssd` if that matters to you.

SSH in. IAP works without opening any firewall port:

```bash
gcloud compute ssh sonicvs-kne --project YOUR_PROJECT --zone us-central1-f --tunnel-through-iap
```

Check KVM before installing anything. Both must pass:

```bash
grep -cw vmx /proc/cpuinfo    # a number greater than 0
ls -l /dev/kvm                # must exist
```

If `vmx` is 0, stop. The VM was created without the flag or on the wrong
machine family. We recreated the VM at this point.

---

## 3. Install host packages

```bash
sudo apt-get update
sudo apt-get install -y docker.io make gcc libpcap-dev build-essential \
  python3 python3-venv git qemu-utils sshpass wget curl

sudo usermod -aG docker "$USER"
sudo usermod -aG kvm "$USER"
```

Log out and back in so the groups apply. If `docker` still says
`permission denied`, run `newgrp docker` once, or use `sudo docker`.

Notes:

- `qemu-utils` provides `qemu-nbd`, which the switch container uses to write
  the switch's identity into its disk before boot.
- `sshpass` is used later to run commands on the switches non-interactively.
- The README of some switch images mentions a `linux-modules-extra-<kernel>`
  package. It **does not exist** for this GCP kernel, and it is not needed: the
  `nbd` module is already in `linux-modules-7.0.0-1017-gcp`.

---

## 4. Kernel modules and sysctls

Three things have to be true on the VM, and all three bit us at some point.

### 4a. `nbd` (the switch container needs it)

```bash
sudo modprobe nbd max_part=8
echo nbd | sudo tee /etc/modules-load.d/nbd.conf
echo "options nbd max_part=8" | sudo tee /etc/modprobe.d/nbd.conf
lsmod | grep nbd
ls -l /dev/nbd0
```

`modprobe` prints nothing when it works. The kind node shares the host kernel,
so it also has to see the module (done in step 7 once the node exists).

### 4b. `br_netfilter` (the cluster's DNS breaks without it)

```bash
sudo modprobe br_netfilter
echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf
printf "net.bridge.bridge-nf-call-iptables = 1\nnet.bridge.bridge-nf-call-ip6tables = 1\n" \
  | sudo tee /etc/sysctl.d/99-kube-bridge.conf
```

Why: pods in this kind cluster sit on a plain Linux bridge. Without
`br_netfilter`, a reply from one pod to another on that bridge never passes
through iptables, so kube-proxy's NAT is not undone. The result is that a pod
can reach CoreDNS by pod IP but **not** through the `kube-dns` service
(`10.96.0.10`), so every service-name lookup times out. See
[Troubleshooting](#16-troubleshooting).

The `/proc/sys/net/bridge/` entries only exist once the module is loaded. After
the kind node exists (step 6), also run:

```bash
sudo docker exec kne-control-plane sysctl -w net.bridge.bridge-nf-call-iptables=1
```

### 4c. Limits for many pods

```bash
sudo tee /etc/sysctl.d/99-kne.conf <<'EOF'
fs.inotify.max_user_instances = 1280
fs.inotify.max_user_watches = 655360
net.core.somaxconn = 4096
fs.file-max = 9223372036854775807
EOF
sudo sysctl --system
```

---

## 5. Install Go, kubectl, kind and KNE

```bash
# Go (see https://go.dev/doc/install). Verified: 1.25.4
echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> ~/.bashrc
source ~/.bashrc

# kubectl (verified: v1.37.2)
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# kind (verified: v0.27.0)
go install sigs.k8s.io/kind@v0.27.0

# KNE
git clone https://github.com/openconfig/kne.git ~/kne
cd ~/kne && make install            # installs /usr/local/bin/kne
kne --help
```

`kne` has no `--version` flag. This document used the checkout at commit
`68d11a4`; if a newer KNE changes the deploy file layout, pin to that commit.

---

## 6. Create the kind cluster

### 6a. Cluster config

The default is 110 pods per node, and each switch pod brings extra interfaces.
This file raises the limit and gives the node a larger pod CIDR. It is only read
when the cluster is created.

```bash
sudo mkdir -p /tmp/kne /data/kne-pv
mkdir -p ~/kne/kind
cat > ~/kne/kind/kind-no-cni.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  kubeadmConfigPatches:
  - |
    kind: InitConfiguration
    nodeRegistration:
      kubeletExtraArgs:
        max-pods: "512"
  - |
    kind: ClusterConfiguration
    controllerManager:
      extraArgs:
        node-cidr-mask-size: "22"
  extraMounts:
  - hostPath: /tmp/kne
    containerPath: /tmp/kne
  - hostPath: /data/kne-pv
    containerPath: /var/local-path-provisioner
EOF
```

There is exactly one node and it is the control plane. That is why every link in
this lab is a veth pair inside the one node; the VXLAN/gRPC paths in meshnet are
never used.

### 6b. Deploy

This is the deployment file we used. It lives in the KNE checkout and points at
the config above:

```bash
cd ~/kne
kne deploy cmd/deploy/testdata/kind-deployment.yaml
```

It installs, in order: the kind cluster `kne` (node image `kindest/node:v1.26.0`),
MetalLB (100 addresses), meshnet, and the vendor controllers (IxiaTG, SR Linux,
cEOS, Lemming). The first run pulls many images and takes several minutes.

Check it:

```bash
kind get clusters                      # kne
kubectl get nodes                      # kne-control-plane  Ready
kubectl get pods -A                    # everything Running
kubectl get ipaddresspool -n metallb-system \
  -o jsonpath='{.items[0].spec.addresses}'     # ["172.18.0.50 - 172.18.0.150"]
```

Make the cluster come back after a VM reboot:

```bash
docker update --restart=unless-stopped kne-control-plane
```

(`kne deploy` uses `recycle: True`, so re-running it reuses the existing cluster.)

---

## 7. Prepare the kind node

### 7a. Load `nbd` inside the node

```bash
sudo docker exec kne-control-plane modprobe nbd max_part=8
```

This is lost if the node is recreated. Re-run it after any `kind delete`.

### 7b. (Optional) a patched meshnet

The VM we verified runs a **patched meshnet image**, `meshnet-patched:v0.3.2`,
because the stock meshnet stalls on large topologies. The patch comes from a
separate repo and is not reproduced here. For a three-switch lab we have **not
tested** the stock meshnet that `kne deploy` installs; start with that and only
patch if pods stall in `Init`.

If you do patch it, the image is rolled in with:

```bash
kubectl set image daemonset/meshnet meshnet=meshnet-patched:v0.3.2 -n meshnet
kubectl rollout restart daemonset/meshnet -n meshnet
kubectl rollout status daemonset/meshnet -n meshnet
```

---

## 8. Sanity check with two plain containers (optional, 2 minutes)

This proves meshnet can cable two pods before you involve a switch. The topology
is the smallest possible: two Alpine pods and one link.

```bash
cat > two-hosts.pb.txt <<'EOF'
name: "play-two-hosts"
nodes: { name: "a" vendor: HOST config: { image: "alpine:3.22" } }
nodes: { name: "b" vendor: HOST config: { image: "alpine:3.22" } }
links: { a_node: "a" a_int: "eth1" z_node: "b" z_int: "eth1" }
EOF

# kind load is unreliable here (see step 9), so pull into the node directly:
sudo docker exec kne-control-plane crictl pull docker.io/library/alpine:3.22

kne create two-hosts.pb.txt
kubectl wait -n play-two-hosts --for=condition=Ready pod --all --timeout=180s
kubectl exec -n play-two-hosts a -- ip link set eth1 up
kubectl exec -n play-two-hosts a -- ip addr add 10.0.1.1/24 dev eth1
kubectl exec -n play-two-hosts b -- ip link set eth1 up
kubectl exec -n play-two-hosts b -- ip addr add 10.0.1.2/24 dev eth1
kubectl exec -n play-two-hosts a -- ping -c 3 10.0.1.2
kne delete two-hosts.pb.txt
```

`kne create` only creates the wire. The addresses are added afterwards. `eth0`
is the Kubernetes network; the topology links start at `eth1`.

---

## 9. Build the switch image and get it into the kind node

Build your switch container on the VM (your own Dockerfile and disk image):

```bash
docker build --no-cache -t YOUR_SWITCH_IMAGE:latest .
docker images | grep YOUR_SWITCH_IMAGE
```

### Why `kind load` is not used

`kind load docker-image` failed on this VM with
`ctr: content digest ... not found`. Rather than debug it, we serve the image
from a registry on the VM and pull it into the node with `crictl`. This worked:

```bash
# 1. a local registry on the VM
docker run -d --restart=always -p 5000:5000 --name registry registry:2
docker tag YOUR_SWITCH_IMAGE:latest localhost:5000/YOUR_SWITCH_IMAGE:latest
docker push localhost:5000/YOUR_SWITCH_IMAGE:latest

# 2. let the node's containerd pull from it over plain HTTP.
#    172.17.0.1 is the docker0 address, reachable from inside the kind node.
sudo docker exec kne-control-plane bash -c '
mkdir -p /etc/containerd/certs.d/172.17.0.1:5000
cat > /etc/containerd/certs.d/172.17.0.1:5000/hosts.toml <<EOF
server = "http://172.17.0.1:5000"
[host."http://172.17.0.1:5000"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
EOF
systemctl restart containerd
'

# 3. pull it into the node and give it the name the topology uses
sudo docker exec kne-control-plane crictl pull 172.17.0.1:5000/YOUR_SWITCH_IMAGE:latest
sudo docker exec kne-control-plane ctr -n k8s.io images tag \
  172.17.0.1:5000/YOUR_SWITCH_IMAGE:latest docker.io/library/YOUR_SWITCH_IMAGE:latest

sudo docker exec kne-control-plane crictl images | grep YOUR_SWITCH_IMAGE
```

Do this **before** creating a topology: restarting containerd in the node while
pods are running is disruptive. You should see the image under both names.

### Pre-pull the Ixia-c images (recommended)

The first Ixia-c deploy downloads several images and is slow. Pull them into the
node first. The exact tags are in the KNE checkout, in
`manifests/keysight/ixiatg-configmap.yaml`; these were the ones used:

```bash
for img in \
  ghcr.io/open-traffic-generator/keng-controller:1.61.0-1 \
  ghcr.io/open-traffic-generator/otg-gnmi-server:1.61.0 \
  ghcr.io/open-traffic-generator/ixia-c-traffic-engine:1.8.0.544 \
  ghcr.io/open-traffic-generator/ixia-c-protocol-engine:1.00.0.534
do sudo docker exec kne-control-plane crictl pull $img; done
```

---

## 10. Deploy the topology

Copy [`topology/leaf-spine-otg.pb.txt`](topology/leaf-spine-otg.pb.txt) to the
VM, replace `YOUR_SWITCH_IMAGE:latest`, then:

```bash
kne create leaf-spine-otg.pb.txt
kubectl get pods -n 20 -o wide
kubectl get svc -n 20
```

It took about 3 minutes here. Expected pods in namespace `20`:

```
leaf1            1/1   Running
leaf2            1/1   Running
spine1           1/1   Running
otg-controller   2/2   Running
otg-port-eth1    2/2   Running
otg-port-eth2    2/2   Running
```

Remember `-n 20`. Everything lives in the namespace named by the topology file.
There is no pod called `otg`: `otg` is an `IxiaTG` custom resource that the
Keysight operator expands into the three `otg-*` pods and five services
(`kubectl get ixiatg,svc -n 20`).

The five services are `LoadBalancer`s on MetalLB addresses, reachable **from the
VM only**:

| Service | Address | Used for |
|---|---|---|
| `service-https-otg-controller` | `172.18.0.50:8443` | REST API. snappi talks to this. |
| `service-gnmi-otg-controller` | `172.18.0.51:50051` | gNMI metrics (optional) |
| `service-grpc-otg-controller` | `172.18.0.52:40051` | controller gRPC (not used here) |
| `service-otg-port-eth1` | `172.18.0.53:5555`, `:50071` | port 1 engine control |
| `service-otg-port-eth2` | `172.18.0.54:5555`, `:50071` | port 2 engine control |

Boot is staggered by the switch image (roughly `20 s x (SWITCH_ID mod 14)`), so
`spine1` (ID 101) starts last. Pods go `Running` quickly, but the switch OS
inside still needs a few more minutes before it answers SSH.

---

## 11. Make the switches reachable from the VM

The switch management IPs are `172.31.<TOPO_ID>.<SWITCH_ID>`:
`172.31.20.1` (leaf1), `172.31.20.2` (leaf2), `172.31.20.101` (spine1). They live
on a bridge inside each pod, which the VM cannot reach by default, and kind
masquerades pod traffic.

Our switch-image repo has two helper scripts that fix this after
`kne create` (they are not in this repo):

```bash
bash docker/fix-masq.sh leaf-spine-otg.pb.txt
python3 engine/setup_routes_on_bm.py leaf-spine-otg.pb.txt --sonic-only
```

What they do, from what we observed:

- **`fix-masq.sh`** adds `RETURN` rules for `172.31.0.0/16` to the
  `KIND-MASQ-AGENT` chain (in `iptables-legacy`, inside the kind node) so the
  switches' management source IPs are not masqueraded, and adds a host route
  `172.31.20.0/24 via <kind-node-ip> dev <kind-docker-bridge>`.
- **`setup_routes_on_bm.py`** adds, inside the kind node, one `/32` route per
  switch (`172.31.20.<id> via <pod IP> dev kind-br`), fixes the management
  bridge gateway and NAT inside each pod, and then waits for each switch to
  answer on its management IP. It waits about 3 minutes for the OS to boot.

If you do not have equivalent scripts, you must reproduce those routes
yourself. We have not tried to do it by hand.

Check:

```bash
ping -c 2 172.31.20.1
ping -c 2 172.31.20.2
ping -c 2 172.31.20.101
ssh admin@172.31.20.1      # use your image's admin password
```

---

## 12. Configure the switches

### Port mapping

KNE `ethN` on the pod is a front-panel port inside the switch OS. On this image:

| Pod interface | Switch port | Alias |
|---|---|---|
| `eth1` | `Ethernet0` | `Et1/1` |
| `eth2` | `Ethernet4` | `Et1/2` |

Confirm with `show interfaces status` on your own image. The mapping depends on
the image.

### Addressing plan

```
tester port 1  10.10.1.2/24  --  leaf1   Ethernet0  10.10.1.1/24
leaf1  Ethernet4  10.20.1.1/30  --  spine1  Ethernet0  10.20.1.2/30
spine1 Ethernet4  10.20.2.2/30  --  leaf2   Ethernet4  10.20.2.1/30
leaf2  Ethernet0  10.10.2.1/24  --  tester port 2  10.10.2.2/24
```

### Commands (standard SONiC `config`)

These are the exact commands applied. Run each block over SSH on that switch.

**leaf1** (`172.31.20.1`)

```
sudo config interface ip add Ethernet0 10.10.1.1/24
sudo config interface ip add Ethernet4 10.20.1.1/30
sudo config route add prefix 10.10.2.0/24 nexthop 10.20.1.2
sudo config route add prefix 10.20.2.0/30 nexthop 10.20.1.2
```

**spine1** (`172.31.20.101`)

```
sudo config interface ip add Ethernet0 10.20.1.2/30
sudo config interface ip add Ethernet4 10.20.2.2/30
sudo config route add prefix 10.10.1.0/24 nexthop 10.20.1.1
sudo config route add prefix 10.10.2.0/24 nexthop 10.20.2.1
```

**leaf2** (`172.31.20.2`)

```
sudo config interface ip add Ethernet0 10.10.2.1/24
sudo config interface ip add Ethernet4 10.20.2.1/30
sudo config route add prefix 10.10.1.0/24 nexthop 10.20.2.2
sudo config route add prefix 10.20.1.0/30 nexthop 10.20.2.2
```

What each pair does: `interface ip add` gives the port an address (and a
connected route for its subnet). `route add ... nexthop` tells the switch which
neighbour to send a subnet to when it is not directly attached.

### Verify leaf1 to leaf2

From leaf1:

```
show ip interfaces
show ip route | grep 10.10
ping -c 4 10.10.2.1
```

Expected: both data ports `up/up`, a connected route for `10.10.1.0/24` and a
static route `10.10.2.0/24 via 10.20.1.2`, and the ping succeeds with **TTL 63**
(one routed hop through the spine).

### These changes are not saved

They exist only in the running config, so a switch reboot wipes them. To save
them to the switch's startup config:

```
sudo config save -y
```

We did not run this, and we did not test whether saved config survives a pod
restart. That depends on whether your image keeps its switch disk.

---

## 13. Run the traffic test

The test uses [snappi](https://pypi.org/project/snappi/), the Python client for
the Open Traffic Generator (OTG) API. It talks only to the Ixia-c controller.
Run it **on the VM**; the `172.18.0.x` addresses are not reachable from
anywhere else.

```bash
python3 -m venv ~/otg-venv
~/otg-venv/bin/pip install snappi
~/otg-venv/bin/python scripts/otg_test.py
```

What [`scripts/otg_test.py`](scripts/otg_test.py) does:

1. Defines two tester ports (`eth1`, `eth2`), two devices (`10.10.1.2` with
   gateway `10.10.1.1`, and `10.10.2.2` with gateway `10.10.2.1`), and two flows
   (h1 to h2 and h2 to h1), each 1,000 packets of 128 bytes at 100 pps.
2. `set_config` pushes it to `https://172.18.0.50:8443`.
3. `set_control_state` with `start` begins transmission.
4. After about 13 seconds, `get_metrics` reads flow and port counters.
5. `set_control_state` with `stop` ends it.

Two things that are easy to get wrong, both from the controller's own errors:

- A port's `location` must be the **topology interface name** (`eth1`, `eth2`).
  The controller keeps a location map (`eth1` -> `otg-port-eth1`, ...). Passing
  the service IP gives `could not find remapped location name`.
- The **controller** (not the VM) connects to the port pods by their
  `*.svc.cluster.local` names, so cluster DNS must work (step 4b).

Result we got:

```
=== Flow metrics (tester view) ===
flow           tx_frames   rx_frames    lost
h1_to_h2            1000        1000       0
h2_to_h1            1000        1000       0

=== Port metrics (tester view) ===
port     tx_frames   rx_frames
p1            1000        1003
p2            1000        1002
```

### Counting on the switches

The tester can only see its own two ports. To see traffic at each switch, read
the interface counters before and after:

```
show interfaces counters -i Ethernet0
show interfaces counters -i Ethernet4
```

Change in `RX_OK` / `TX_OK` over our run:

| Switch | Port | Faces | RX | TX |
|---|---|---|---|---|
| leaf1 | Ethernet0 | tester port 1 | +1,002 | +1,021 |
| leaf1 | Ethernet4 | spine1 | +1,022 | +1,021 |
| spine1 | Ethernet0 | leaf1 | +1,021 | +1,021 |
| spine1 | Ethernet4 | leaf2 | +1,021 | +1,021 |
| leaf2 | Ethernet4 | spine1 | +1,021 | +1,021 |
| leaf2 | Ethernet0 | tester port 2 | +1,002 | +1,021 |

Each hop carries the 1,000 test frames in each direction. The extra ~20 frames
per port are background traffic; we assume ARP and control packets and did not
verify that. The spine-facing ports also showed about 19 new `RX_DRP` between
the two readings. The flows lost nothing, so these drops are not test traffic;
we did not identify the cause.

---

## 14. Reach the cluster from your laptop (optional)

The Kubernetes API listens on `127.0.0.1` on the VM, so no GCP firewall rule is
needed. Use an SSH tunnel.

```bash
# on the VM
kind get kubeconfig --name kne > kne.yaml
grep server kne.yaml           # https://127.0.0.1:<PORT>
```

```bash
# on your laptop
scp YOUR_USER@VM:kne.yaml ~/.kube/kne.yaml && chmod 600 ~/.kube/kne.yaml
ssh -N -L <PORT>:127.0.0.1:<PORT> YOUR_USER@VM_EXTERNAL_IP
KUBECONFIG=~/.kube/kne.yaml kubectl get pods -n 20
```

Only the cluster API is tunnelled. The Ixia-c addresses (`172.18.0.x`) and the
switch management IPs (`172.31.20.x`) are still VM-only, so run the test and the
switch commands on the VM.

---

## 15. Teardown and cost

Remove only the topology (the cluster stays):

```bash
kne delete leaf-spine-otg.pb.txt
kubectl get ns 20          # NotFound
```

**Do not run `kne teardown`:** it deletes the whole kind cluster.

Stop paying for compute when idle:

```bash
gcloud compute instances stop sonicvs-kne --zone us-central1-f
```

Disks keep billing while the VM is stopped. The kind node has restart policy
`unless-stopped` and the local registry has `always`, so both should come back
after `start`. We have **not tested** a VM stop/start. Assume the switches reboot
and that unsaved switch config (step 12) is lost.

---

## 16. Troubleshooting

Rows marked with † come from the switch image's own notes. We did not hit them.

| Symptom | Cause | Fix |
|---|---|---|
| `grep -cw vmx` is 0, no `/dev/kvm` | VM created without nested virtualization, or not an Intel N2 | Recreate the VM with the flag (step 2). That is what we did. |
| `linux-modules-extra-<ver>` not found | Package does not exist for this GCP kernel | Skip it. `nbd` is already in the kernel package. |
| `docker` permission denied | Group not active in this shell | Log out/in, `newgrp docker`, or `sudo docker`. |
| `kind load docker-image` fails: `content digest ... not found` | containerd import issue on this VM | Use the registry + `crictl pull` method (step 9). |
| † Pods in `Init` for a long time | meshnet not wiring | `kubectl logs -n meshnet -l app=meshnet`; consider the patched meshnet (step 7b). |
| † Switch pod runs but is very slow | No `/dev/kvm` inside the pod | Check step 2; the pod must be privileged. |
| † `qemu-nbd` fails in the switch pod | `nbd` not loaded in the host or node | Step 4a and step 7a. |
| `kubectl get pods` shows nothing | Wrong namespace | Use `-n 20` (or `-A`). |
| snappi `set_config`: `could not find remapped location name` | Port location is an IP | Use `eth1` / `eth2` (step 13). |
| snappi `set_config` 500: `could not create traffic service for eth2 ... lookup ... i/o timeout` | Cluster DNS not working | Load `br_netfilter` and set the sysctl (step 4b). |
| `nslookup` via `10.96.0.10` times out, but CoreDNS answers on its pod IP | `br_netfilter` missing, so service NAT is not undone for same-bridge replies | Step 4b. Relaxing `rp_filter` did **not** fix it. |
| `kubectl exec ... bash` fails on Alpine | Alpine has no bash | `kubectl exec -it -n NS POD -- sh` |
| Switch ping works but tester sees loss | Routes missing on one switch | `show ip route` on all three switches; check both directions. |

---

## 17. What is not covered

- **BGP.** The fabric uses static routes. BGP (eBGP leaf-to-spine, each leaf
  advertising its tester subnet) would replace them, and it pays off mainly once
  there is a second spine. The static routes must be removed first, or they win
  by administrative distance. Not configured here.
- **A second spine** (ECMP) and larger fabrics.
- **Saving switch config** across reboots.
- **Line-rate testing.** Ixia-c here is software. It is meant for functional and
  loss checks, bounded by the VM's CPU and the veth/QEMU path. We have not
  measured its limits.
- **Building the switch image** itself. That depends on your NOS.
