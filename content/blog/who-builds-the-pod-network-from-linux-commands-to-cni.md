---
external: false
featured: true
title: "Who Builds the Pod Network? From Linux Commands to CNI"
description: "Part 3 of Kubernetes Networking Under the Hood: use CNI with cnitool, the bridge plugin, and host-local IPAM to create the same Pod-like Linux network we built by hand."
date: 2026-08-15
series: "Kubernetes Networking Under the Hood"
seriesPart: 3
categories:
  - Kubernetes
tags:
  - linux
  - networking
  - cni
  - cka
references:
  - title: "CNI Specification"
    url: "https://www.cni.dev/docs/spec/"
  - title: "CNI bridge plugin"
    url: "https://www.cni.dev/plugins/current/main/bridge/"
  - title: "CNI host-local IPAM plugin"
    url: "https://www.cni.dev/plugins/current/ipam/host-local/"
  - title: "cnitool"
    url: "https://github.com/containernetworking/cni/tree/main/cnitool"
  - title: "Kubernetes Container Runtime Interface"
    url: "https://kubernetes.io/docs/concepts/architecture/cri/"
---

In the first two parts, we kept Kubernetes out of the picture.

We created network namespaces, veth pairs, bridges, IP addresses, and routes ourselves. Then we moved beyond a single local network and followed packets through gateways, Linux routing, forwarding, and NAT.

So far, every piece of the network has been configured by hand.

For each new Pod-like namespace, we had to:

```text
create namespace
      |
create veth pair
      |
move one end into namespace
      |
attach the other end to bridge
      |
assign an IP address
      |
configure routes
```

Kubernetes needs the same kind of networking setup whenever a Pod is created on a node, but that work is automated.

This is where CNI fits.

We briefly introduced CNI in the first article. Here, we will use it directly.

We will create an empty network namespace, connect it to a network through CNI, and inspect the Linux objects that appear.

## What is CNI?

CNI stands for **Container Network Interface**.

It is a specification for how a runtime asks networking plugins to configure a network namespace.

The runtime provides information such as:

* which network namespace to configure
* what the interface inside it should be called
* which operation should be performed
* the network configuration itself

A plugin then performs the networking work.

A simple mental model is:

```text
runtime
   |
   | CNI
   v
plugin
   |
   v
Linux networking
```

**CNI is the contract. The plugin is the implementation.**

For this lab, we will use two plugins from the official CNI plugins project.

The first is the `bridge` plugin.

It creates a veth pair, places one end inside the target network namespace, and connects the host end to a Linux bridge.

That is very close to the network we built manually in Part 1.

The second is `host-local`, an IP Address Management plugin, usually shortened to IPAM.

It chooses an available IP address from a configured range and keeps track of existing allocations.

Our setup will look roughly like this:

```text
             CNI configuration
                    |
          +---------+---------+
          |                   |
          v                   v
   bridge plugin       host-local IPAM
          |                   |
          | create veth       | allocate IP
          | attach bridge     |
          +---------+---------+
                    |
                    v
             network namespace
```

Using the bridge plugin gives us a useful comparison with Part 1 because we already know what the resulting Linux network should look like.

## Calling CNI with cnitool

When a runtime asks a CNI plugin to add a container to a network, the plugin receives information similar to:

```text
CNI_COMMAND=ADD
CNI_CONTAINERID=...
CNI_NETNS=/run/netns/pod-a
CNI_IFNAME=eth0
```

It also receives the network configuration as JSON.

For this lab, the two operations we care about are:

```text
ADD
 |
 | attach networking
 v

DEL
 |
 | remove networking
 v
```

CNI defines other operations as well, but `ADD` and `DEL` are enough for the lifecycle we want to look at here.

We could invoke the plugin binaries directly and provide those inputs ourselves, but `cnitool` gives us a simpler way to do it. It applies a CNI configuration to an existing network namespace.

A container runtime would normally sit above CNI:

```text
container runtime
       |
       | CNI ADD
       v
    plugins
       |
       v
Linux networking
```

For this lab, `cnitool` takes the runtime's place:

```text
    cnitool
       |
       | CNI ADD
       v
    plugins
       |
       v
Linux networking
```

That lets us inspect the CNI layer without needing a Kubernetes cluster or container runtime.

## Before starting

I recommend using a disposable Linux VM.

I ran the lab inside a privileged Debian Bookworm container on Docker Desktop.

That environment also exposes a few kernel tunnel interfaces inside otherwise empty network namespaces. I leave those out of the outputs because they are unrelated to this lab. On a typical disposable VM, you may only see `lo`.

If you already have `cnitool` and the official CNI plugins installed, you can skip most of this section.

On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y iproute2 iputils-ping git golang-go
```

Install `cnitool`:

```bash
go install github.com/containernetworking/cni/cnitool@latest
sudo install "$(go env GOPATH)/bin/cnitool" /usr/local/bin/cnitool
```

Check that it is available:

```bash
cnitool --help
```

Build the official CNI plugins:

```bash
git clone https://github.com/containernetworking/plugins.git
cd plugins
./build_linux.sh
```

For this lab, we only need:

```text
bridge
host-local
```

Copy them into a conventional CNI plugin directory:

```bash
sudo mkdir -p /opt/cni/bin
sudo cp bin/bridge bin/host-local /opt/cni/bin/
```

Create the configuration directory:

```bash
sudo mkdir -p /etc/cni/net.d
```

## Step 1: Create an empty network namespace

Create the first namespace:

```bash
sudo ip netns add pod-a
```

Inspect it:

```bash
sudo ip -n pod-a -br link
```

On my lab:

```text
lo               DOWN           00:00:00:00:00:00 <LOOPBACK>
```

There is no `eth0`, Pod IP, host-side veth, or bridge yet.

```text
Linux host

    pod-a
      |
      X
```

In Part 1, we started building those pieces manually from here.

For this lab, the namespace is all we create ourselves.

## Step 2: Describe the network

Create `/etc/cni/net.d/10-tiny-net.conf`:

```bash
sudo tee /etc/cni/net.d/10-tiny-net.conf > /dev/null <<'EOF'
{
  "cniVersion": "1.0.0",
  "name": "tiny-net",
  "type": "bridge",
  "bridge": "cni0",
  "isGateway": true,
  "ipam": {
    "type": "host-local",
    "ranges": [
      [
        {
          "subnet": "10.10.0.0/24"
        }
      ]
    ]
  }
}
EOF
```

The important parts are:

```text
"type": "bridge"
        |
        +--> use the bridge plugin

"bridge": "cni0"
        |
        +--> connect the namespace to this bridge

"isGateway": true
        |
        +--> let the bridge act as the subnet gateway

"ipam": {
  "type": "host-local"
}
        |
        +--> allocate IP addresses locally

"subnet": "10.10.0.0/24"
        |
        +--> allocate them from this network
```

At this point, we have only created a configuration file.

The bridge does not exist yet:

```bash
ip link show cni0
```

```text
Device "cni0" does not exist.
```

And `pod-a` still has no `eth0`.

```bash
sudo ip -n pod-a -br addr
```

The configuration describes the network. The plugins have not applied it yet.

## Step 3: Run CNI ADD

Run:

```bash
sudo CNI_PATH=/opt/cni/bin \
  cnitool add tiny-net /var/run/netns/pod-a
```

In my lab, `cnitool` returned:

```json
{
  "cniVersion": "1.0.0",
  "interfaces": [
    {
      "name": "cni0"
    },
    {
      "name": "veth6b96cdeb"
    },
    {
      "name": "eth0",
      "sandbox": "/var/run/netns/pod-a"
    }
  ],
  "ips": [
    {
      "address": "10.10.0.2/24",
      "gateway": "10.10.0.1",
      "interface": 2
    }
  ]
}
```

I removed the MAC addresses because they are not relevant here.

The result already tells us what was configured:

```text
cni0
host-side veth
pod-a / eth0
```

and the allocated address:

```text
10.10.0.2/24
gateway 10.10.0.1
```

We did not create any of those interfaces manually.

Let's inspect them.

## Step 4: Inspect the namespace and host

Inside `pod-a`:

```bash
sudo ip -n pod-a -br addr
```

```text
lo               DOWN
eth0@if12        UP             10.10.0.2/24 ...
```

`lo` is still down because the `bridge` plugin only configured the interface we asked it to create, `eth0`. It does not configure loopback. The official CNI plugins project has a separate `loopback` plugin for that job. We do not need it for this lab.

The `...` hides the IPv6 link-local address that Linux also assigned.

The route table contains the directly connected subnet:

```bash
sudo ip -n pod-a route
```

```text
10.10.0.0/24 dev eth0 proto kernel scope link src 10.10.0.2
```

On the host:

```bash
ip -br addr show cni0
```

```text
cni0             UP             10.10.0.1/24 ...
```

Before `CNI ADD`, `cni0` did not exist.

The bridge also has a veth attached:

```bash
bridge link
```

On my machine:

```text
12: veth6b96cdeb@cni0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 master cni0 state forwarding priority 32 cost 2
```

The interface name and index will differ on another machine.

The important part is:

```text
master cni0
```

Our topology is now:

```text
                  Linux host

                    cni0
                10.10.0.1
                     |
               veth6b96cdeb
                     |
                    eth0
                10.10.0.2
                   pod-a
```

## What did CNI automate?

This is almost the same network we built manually in Part 1.

| Part 1                              | This time                   |
| ----------------------------------- | --------------------------- |
| Created the veth pair               | `bridge` plugin created it  |
| Moved one end into the namespace    | `bridge` plugin moved it    |
| Attached the host end to the bridge | `bridge` plugin attached it |
| Assigned an IP manually             | `host-local` allocated one  |
| Configured the namespace interface  | plugin configured `eth0`    |

**The abstraction changed. The Linux objects did not.**

The resulting network still consists of familiar pieces:

```text
namespace
   |
  eth0
   |
  veth
   |
  cni0
```

CNI automated their creation and configuration.

## Step 5: Where did 10.10.0.2 come from?

We gave `host-local` this range:

```text
10.10.0.0/24
```

In a clean lab, the first namespace received:

```text
10.10.0.2
```

The allocator needs to remember that address before another namespace is added.

By default, `host-local` stores allocation state under:

```text
/var/lib/cni/networks/<network-name>
```

For `tiny-net`:

```bash
sudo ls /var/lib/cni/networks/tiny-net
```

```text
10.10.0.2
last_reserved_ip.0
lock
```

The file named after the IP represents the active allocation.

```bash
sudo cat /var/lib/cni/networks/tiny-net/10.10.0.2
```

On my lab:

```text
cnitool-af0507dddb173175b8b2
eth0
```

The allocator also keeps bookkeeping about where allocation last stopped:

```bash
sudo cat /var/lib/cni/networks/tiny-net/last_reserved_ip.0
```

```text
10.10.0.2
```

So IPAM is not simply calculating an address. It keeps local allocation state.

## Step 6: Add pod-b

Create another namespace:

```bash
sudo ip netns add pod-b
```

Before CNI touches it:

```bash
sudo ip -n pod-b -br addr
```

There is no `eth0`.

Apply the same network configuration:

```bash
sudo CNI_PATH=/opt/cni/bin \
  cnitool add tiny-net /var/run/netns/pod-b
```

The second allocation in my clean lab was:

```text
10.10.0.3/24
gateway 10.10.0.1
```

Inside `pod-b`:

```bash
sudo ip -n pod-b -br addr
```

```text
eth0@if13        UP             10.10.0.3/24 ...
```

IPAM now has two active allocations:

```bash
sudo ls /var/lib/cni/networks/tiny-net
```

```text
10.10.0.2
10.10.0.3
last_reserved_ip.0
lock
```

The topology is now:

```text
                    Linux host

                       cni0
                   10.10.0.1
                    /       \
                   /         \
              vethXXXX     vethYYYY
                 |             |
                eth0          eth0
             10.10.0.2     10.10.0.3
               pod-a         pod-b
```

This is essentially the network from Part 1, but the CNI plugins configured it for us.

## Step 7: Test communication

From `pod-a`:

```bash
sudo ip netns exec pod-a ping -c 3 10.10.0.3
```

My output:

```text
64 bytes from 10.10.0.3: icmp_seq=1 ttl=64 time=0.063 ms
64 bytes from 10.10.0.3: icmp_seq=2 ttl=64 time=0.149 ms
64 bytes from 10.10.0.3: icmp_seq=3 ttl=64 time=0.056 ms
```

The TTL is still `64`.

The namespaces are in the same subnet and communicate through the bridge, so there is no router between them.

The neighbour table confirms that `pod-a` reaches `pod-b` directly:

```bash
sudo ip -n pod-a neigh
```

```text
10.10.0.3 dev eth0 lladdr 76:03:7d:ef:02:52 REACHABLE
```

We can also inspect the bridge forwarding database:

```bash
bridge fdb show br cni0
```

The packet path is the same one we examined in Part 1. What changed is how that network was configured.

## Step 8: Remove pod-b from the network

CNI also has to deal with network cleanup when a workload disappears.

Before removing `pod-b`:

```bash
sudo ip -n pod-b -br addr
```

```text
eth0@if13        UP             10.10.0.3/24 ...
```

Run:

```bash
sudo CNI_PATH=/opt/cni/bin \
  cnitool del tiny-net /var/run/netns/pod-b
```

Then inspect the namespace again:

```bash
sudo ip -n pod-b -br link
```

`eth0` is gone.

The namespace itself is still present:

```bash
ip netns list
```

That is because we created the namespace ourselves. CNI only managed its network attachment.

```text
namespace lifecycle
        !=
network attachment lifecycle
```

IPAM also released the address:

```bash
sudo ls /var/lib/cni/networks/tiny-net
```

```text
10.10.0.2
last_reserved_ip.0
lock
```

The `10.10.0.3` allocation file is gone.

`last_reserved_ip.0` still contains:

```text
10.10.0.3
```

That does not mean the address is still allocated. The active allocation file has been removed; `last_reserved_ip.0` is allocator bookkeeping.

The host-side veth belonging to `pod-b` is also gone, while `pod-a` remains connected to the shared bridge.

## Where Kubernetes fits

So far, we have called CNI ourselves.

On a Kubernetes node, kubelet talks to the container runtime through the **Container Runtime Interface**, or CRI.

As part of setting up a Pod sandbox, the runtime is responsible for configuring its networking, commonly through CNI plugins.

A simplified view is:

```text
kubelet
   |
   | CRI
   v
container runtime
   |
   | CNI
   v
network plugins
   |
   v
Linux networking
```

Our lab removed the upper layers:

```text
Real Kubernetes node:

kubelet
   |
runtime
   |
  CNI
   |
plugins
   |
Linux


Our lab:

cnitool
   |
  CNI
   |
plugins
   |
Linux
```

That is the connection between this lab and the Linux networking from the first two articles.

The bridge, veth pairs, addresses, routes, neighbour entries, and packet forwarding are still Linux networking.

CNI gives the runtime a standard way to delegate network setup to plugins.

## Cleaning up

Remove `pod-a` from the network:

```bash
sudo CNI_PATH=/opt/cni/bin \
  cnitool del tiny-net /var/run/netns/pod-a
```

If `pod-b` is still attached for any reason:

```bash
sudo CNI_PATH=/opt/cni/bin \
  cnitool del tiny-net /var/run/netns/pod-b
```

After both attachments were removed in my lab, `cni0` was still present:

```bash
ip -br addr show cni0
```

```text
cni0             DOWN           10.10.0.1/24 ...
```

IPAM also kept its bookkeeping files:

```bash
sudo ls /var/lib/cni/networks/tiny-net
```

```text
last_reserved_ip.0
lock
```

The individual allocation files were gone.

Delete the namespaces:

```bash
sudo ip netns delete pod-a
sudo ip netns delete pod-b
```

Remove the CNI configuration:

```bash
sudo rm /etc/cni/net.d/10-tiny-net.conf
```

Remove the bridge:

```bash
sudo ip link delete cni0 2>/dev/null || true
```

Clear the remaining allocator state:

```bash
sudo rm -rf /var/lib/cni/networks/tiny-net
```

Verify:

```bash
ip link show cni0
```

```text
Device "cni0" does not exist.
```

## Final thoughts

In the first article, we built a Pod-like network ourselves.

In the second, we looked at what happens when traffic needs to move beyond that local network.

Here, we let CNI plugins create the local networking that we previously configured by hand.

The resulting Linux objects were the same:

```text
veth
bridge
IP address
routes
```

The layers now fit together like this:

```text
Kubernetes
    |
   CRI
    |
runtime
    |
   CNI
    |
plugins
    |
Linux
```

A Pod can now receive an interface, an IP address, and a network path without us manually configuring those pieces.

But a Pod IP is tied to that Pod. Pods can be replaced or rescheduled, and their replacements can receive different addresses.

If an application has several replicas:

```text
10.10.0.12
10.10.0.18
10.10.0.27
```

another application should not have to know which individual Pod IP to use or track how those addresses change.

Kubernetes Services give us a stable way to reach a changing set of Pods.

That is where we will continue next. Stay tuned!
