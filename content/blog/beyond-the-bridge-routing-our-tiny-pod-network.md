---
external: false
featured: true
title: "Beyond the Bridge: Routing Our Tiny Pod Network"
description: "Part 2 of Kubernetes Networking Under the Hood: route traffic between separate Pod-like subnets, enable Linux IP forwarding, and add NAT for internet access."
date: 2026-08-09
series: "Kubernetes Networking Under the Hood"
seriesPart: 2
categories:
  - Kubernetes
tags:
  - linux
  - networking
  - routing
  - nat
references:
  - title: "ip-route(8)"
    url: "https://man7.org/linux/man-pages/man8/ip-route.8.html"
  - title: "Linux IP sysctl documentation"
    url: "https://docs.kernel.org/networking/ip-sysctl.html"
  - title: "iptables-extensions(8)"
    url: "https://man7.org/linux/man-pages/man8/iptables-extensions.8.html"
  - title: "Netfilter conntrack documentation"
    url: "https://docs.kernel.org/networking/nf_conntrack-sysctl.html"
  - title: "Kubernetes cluster networking"
    url: "https://kubernetes.io/docs/concepts/cluster-administration/networking/"
---

In the first part, we built a tiny Pod-like network using Linux network namespaces, veth pairs, and a bridge.

If you missed it, you can find it here: [Build a Tiny Pod Network with Linux](https://levent.dev/blog/build-a-tiny-pod-network-with-linux/).

We ended up with two isolated network namespaces connected to the same Linux bridge:

```text
pod-a
  |
veth pair
  |
Linux bridge
  |
veth pair
  |
pod-b
```

Both network namespaces belonged to the same subnet.

That made the setup relatively simple. When `pod-a` wanted to reach `pod-b`, it could use ARP to find the destination MAC address, and the bridge could forward the Ethernet frame directly.

But real networks rarely remain one large Ethernet segment forever.

Imagine a company where employee devices belong to one network and internal servers belong to another. Or an application network that is kept separate from the database network.

Machines inside each network can communicate through their own switches. The interesting problem begins when a machine in one network needs to reach a machine in the other.

That is the next step for our tiny network.

We will place `pod-a` and `pod-b` in two separate subnets. Once we do that, they will no longer share the same local Ethernet network, and a bridge will not be enough to carry traffic directly between them.

The packet will need to be handed to something connected to both networks, something that can inspect the destination IP address and decide where to send it next.

That is the problem routing solves.

We will build that path ourselves, introducing gateways, routing tables, and IP forwarding as each one becomes necessary. Then we will extend the same network toward the internet, discover why an outbound route is not always enough, and introduce NAT and masquerading.

Finally, we will bring the model back to Kubernetes and use it to reason about communication between Pods on different nodes.

The setup will still be small enough to build by hand. We are just moving one layer beyond the bridge.

## What are we building?

Our initial topology will look like this:

```text
                     Linux host

              br-a              br-b
          10.10.0.1          10.20.0.1
               |                  |
        veth-a-host        veth-b-host
               |                  |
              eth0               eth0
          10.10.0.2          10.20.0.2
             pod-a              pod-b
```

`pod-a` will belong to `10.10.0.0/24`.

`pod-b` will belong to `10.20.0.0/24`.

The Linux host will be connected to both networks through `br-a` and `br-b`.

At first, the two network namespaces will not be able to reach each other. We will add each missing piece one at a time until traffic begins to flow.

After that, we will extend the path toward the outside world:

```text
pod-a
  |
br-a
  |
Linux routing
  |
NAT / masquerading
  |
host external interface
  |
internet
```

We will use an IP address rather than a domain name for the internet test. DNS is a separate problem and deserves its own explanation.

## Before starting

You need a Linux environment with root access.

The commands in this article change network interfaces, routes, kernel settings, and firewall rules. A disposable virtual machine is the safest place to follow along.

Before starting, check that `10.10.0.0/24` and `10.20.0.0/24` do not already overlap with a local, VPN, or container network on your machine. If they do, use two different private subnets throughout the article.

On Ubuntu or Debian, install the tools we will use:

```bash
sudo apt update
sudo apt install iproute2 iputils-ping tcpdump iptables
```

Before changing anything, check whether IPv4 forwarding is currently enabled:

```bash
cat /proc/sys/net/ipv4/ip_forward
```

You will see either:

```text
0
```

or:

```text
1
```

Linux exposes this kernel setting as a file under `/proc/sys`.

A value of `0` means the host will not forward IPv4 packets between its interfaces. A value of `1` means forwarding is enabled.

Make a note of the current value. We will restore it during cleanup.

This lab begins with forwarding disabled so that we can observe what fails before Linux starts acting as a router. If the current value is `1`, temporarily disable it:

```bash
sudo sysctl -w net.ipv4.ip_forward=0
```

Expected output:

```text
net.ipv4.ip_forward = 0
```

We will enable it again only after the packet reaches the host and gets stuck there.

## Step 1: Build two separate networks

Start by creating two network namespaces:

```bash
sudo ip netns add pod-a
sudo ip netns add pod-b
```

Now create one bridge for each network:

```bash
sudo ip link add br-a type bridge
sudo ip link add br-b type bridge
```

Assign an IP address to each bridge:

```bash
sudo ip addr add 10.10.0.1/24 dev br-a
sudo ip addr add 10.20.0.1/24 dev br-b
```

Bring them up:

```bash
sudo ip link set br-a up
sudo ip link set br-b up
```

The host is now connected to two separate IP networks:

```text
                     Linux host

              br-a              br-b
          10.10.0.1          10.20.0.1
```

The bridges still have no network namespaces connected to them.

### Connect pod-a

Create a veth pair:

```bash
sudo ip link add veth-a-host type veth peer name veth-a
```

Move one end into `pod-a`:

```bash
sudo ip link set veth-a netns pod-a
```

Attach the host-side interface to `br-a` and bring it up:

```bash
sudo ip link set veth-a-host master br-a
sudo ip link set veth-a-host up
```

Configure the other side inside the namespace:

```bash
sudo ip -n pod-a link set veth-a name eth0
sudo ip -n pod-a addr add 10.10.0.2/24 dev eth0
sudo ip -n pod-a link set eth0 up
sudo ip -n pod-a link set lo up
```

Verify the result:

```bash
sudo ip -n pod-a -br addr
```

You should see something similar to:

```text
lo      UNKNOWN    127.0.0.1/8
eth0    UP         10.10.0.2/24
```

### Connect pod-b

Repeat the process for `pod-b`, this time using `br-b` and the second subnet:

```bash
sudo ip link add veth-b-host type veth peer name veth-b

sudo ip link set veth-b netns pod-b

sudo ip link set veth-b-host master br-b
sudo ip link set veth-b-host up

sudo ip -n pod-b link set veth-b name eth0
sudo ip -n pod-b addr add 10.20.0.2/24 dev eth0
sudo ip -n pod-b link set eth0 up
sudo ip -n pod-b link set lo up
```

Verify it:

```bash
sudo ip -n pod-b -br addr
```

Expected output:

```text
lo      UNKNOWN    127.0.0.1/8
eth0    UP         10.20.0.2/24
```

Our topology is now complete:

```text
                     Linux host

              br-a              br-b
          10.10.0.1          10.20.0.1
               |                  |
        veth-a-host        veth-b-host
               |                  |
              eth0               eth0
          10.10.0.2          10.20.0.2
             pod-a              pod-b
```

We can also verify the host-side addresses:

```bash
ip -br addr show br-a
ip -br addr show br-b
```

Expected output:

```text
br-a    UP    10.10.0.1/24
br-b    UP    10.20.0.1/24
```

## Step 2: Try to reach the other network

Before adding anything else, let us try to ping `pod-b` from `pod-a`:

```bash
sudo ip netns exec pod-a ping -c 2 10.20.0.2
```

The result should look similar to this:

```text
ping: connect: Network is unreachable
```

This is our first useful failure.

`pod-a` knows how to reach its own subnet, but nothing beyond it.

Inspect its routing table:

```bash
sudo ip -n pod-a route
```

Expected output:

```text
10.10.0.0/24 dev eth0 proto kernel scope link src 10.10.0.2
```

This route says:

```text
Destination inside 10.10.0.0/24?
Send it directly through eth0.
```

But `10.20.0.2` belongs to a different subnet.

`pod-a` checks its routing table, finds no matching route, and stops before sending the packet.

The bridge never even sees it.

## Step 3: Give unknown destinations somewhere to go

`pod-a` needs a next step for destinations outside `10.10.0.0/24`.

The Linux host owns the address `10.10.0.1` on `br-a`, and that address is directly reachable from `pod-a`.

We can tell `pod-a`:

> When none of your more specific routes match, send the packet to `10.10.0.1`.

Add a default route:

```bash
sudo ip -n pod-a route add default via 10.10.0.1
```

`pod-b` needs a return path too:

```bash
sudo ip -n pod-b route add default via 10.20.0.1
```

Inspect the routing table inside `pod-a` again:

```bash
sudo ip -n pod-a route
```

Expected output:

```text
default via 10.10.0.1 dev eth0
10.10.0.0/24 dev eth0 proto kernel scope link src 10.10.0.2
```

And inside `pod-b`:

```bash
sudo ip -n pod-b route
```

Expected output:

```text
default via 10.20.0.1 dev eth0
10.20.0.0/24 dev eth0 proto kernel scope link src 10.20.0.2
```

A default route is the fallback:

```text
Does a more specific route match?
    |
    +-- Yes: use that route
    |
    +-- No: send the packet to the default gateway
```

For `pod-a`, that gateway is `10.10.0.1`.

For `pod-b`, it is `10.20.0.1`.

Both addresses belong to our Linux host.

Try the ping again:

```bash
sudo ip netns exec pod-a ping -c 2 10.20.0.2
```

This time, you should no longer get `Network is unreachable`.

In a typical setup, however, you still will not receive a reply:

```text
2 packets transmitted, 0 received, 100% packet loss
```

Something changed.

Previously, `pod-a` refused to send the packet because it had no route.

Now it has a route, so the packet reaches the Linux host.

It just does not make it to the other network.

## Step 4: Find where the packet stops

We can watch this with `tcpdump`.

In one terminal, listen on `br-a`:

```bash
sudo tcpdump -n -i br-a icmp
```

In another terminal, run the ping again:

```bash
sudo ip netns exec pod-a ping 10.20.0.2
```

You should see echo requests on `br-a`:

```text
10.10.0.2 > 10.20.0.2: ICMP echo request
```

Now listen on `br-b`:

```bash
sudo tcpdump -n -i br-b icmp
```

Run the ping again.

You will probably see nothing on `br-b`.

The packet arrived at the host through `br-a`, but the host did not pass it to `br-b`.

Our packet currently gets this far:

```text
pod-a
  |
eth0
  |
veth-a-host
  |
br-a
  |
Linux host
  X
br-b
  |
pod-b
```

The host is connected to both networks. It also has routes for both of them:

```bash
ip route show 10.10.0.0/24
ip route show 10.20.0.0/24
```

Expected output:

```text
10.10.0.0/24 dev br-a proto kernel scope link src 10.10.0.1
10.20.0.0/24 dev br-b proto kernel scope link src 10.20.0.1
```

The host knows where both networks are.

Why does it not pass the packet between them?

## Step 5: Turn the Linux host into a router

We have reached the point where the word router becomes useful.

`pod-a` and `pod-b` belong to different networks.

Something connected to both networks needs to receive the packet from one side, inspect its destination IP address, and send it through the correct interface on the other side.

That is what a router does.

In our lab, the Linux host is already connected to both networks:

```text
10.10.0.0/24
      |
    br-a
      |
 Linux host
      |
    br-b
      |
10.20.0.0/24
```

It has the required interfaces.

It has routes for both subnets.

But the host is not forwarding packets between its interfaces yet.

Check the kernel setting again:

```bash
cat /proc/sys/net/ipv4/ip_forward
```

Expected output:

```text
0
```

Enable forwarding:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
```

Expected output:

```text
net.ipv4.ip_forward = 1
```

This tells the kernel that it may forward IPv4 packets arriving through one interface and leaving through another.

We did not need this in the first article.

Back then, both network namespaces belonged to the same Layer 2 network. The bridge moved Ethernet frames between its ports.

Now the destination belongs to another IP network. The host must make a Layer 3 routing decision.

Try the ping again:

```bash
sudo ip netns exec pod-a ping -c 3 10.20.0.2
```

Expected output:

```text
64 bytes from 10.20.0.2: icmp_seq=1 ttl=63 time=0.090 ms
64 bytes from 10.20.0.2: icmp_seq=2 ttl=63 time=0.071 ms
64 bytes from 10.20.0.2: icmp_seq=3 ttl=63 time=0.068 ms
```

The TTL is now `63` rather than `64`.

That is another clue that the packet crossed a router. A router decrements the packet's TTL before forwarding it.

Our complete path now works:

```text
pod-a
  |
eth0
  |
veth-a-host
  |
br-a
  |
Linux routing
  |
br-b
  |
veth-b-host
  |
eth0
  |
pod-b
```

### If the ping still does not work

Some Linux systems have firewall rules that block forwarded traffic.

Inspect the `FORWARD` chain:

```bash
sudo iptables -L FORWARD -n -v
```

You may see a default policy of `DROP`:

```text
Chain FORWARD (policy DROP)
```

For this disposable learning environment, temporarily allow traffic between our two bridges:

```bash
sudo iptables -I FORWARD 1 -i br-a -o br-b -j ACCEPT
sudo iptables -I FORWARD 1 -i br-b -o br-a -j ACCEPT
```

Then try the ping again.

These rules are deliberately limited to the two bridges in our lab. Do not broadly disable the firewall on a real system just to make the example work.

## Step 6: Follow the routed packet

Now that the ping works, let us look at what actually happened.

The packet begins inside `pod-a`:

```text
Source IP:      10.10.0.2
Destination IP: 10.20.0.2
```

`pod-a` checks its routing table.

The destination does not match the local `10.10.0.0/24` route, so the default route wins:

```text
default via 10.10.0.1 dev eth0
```

There is an important detail here.

`pod-a` does not use ARP to find the MAC address of `10.20.0.2`.

That address is not part of its local Ethernet network.

Instead, `pod-a` uses ARP to find the MAC address of its gateway, `10.10.0.1`.

Inspect the neighbour table:

```bash
sudo ip -n pod-a neigh
```

You should see something similar to:

```text
10.10.0.1 dev eth0 lladdr 2a:71:9c:40:18:44 REACHABLE
```

The MAC address will be different on your machine.

At the first network boundary, the packet looks roughly like this:

```text
Ethernet source:      pod-a eth0 MAC
Ethernet destination: br-a MAC

IP source:            10.10.0.2
IP destination:       10.20.0.2
```

The Ethernet frame is addressed to the gateway.

The IP packet inside it is still addressed to `pod-b`.

When the host receives the frame, it removes the Ethernet header and checks the destination IP address.

Its routing table says:

```text
10.20.0.0/24 dev br-b
```

The host now needs to deliver the packet through `br-b`.

If it does not already know the destination MAC address, it sends an ARP request on the second network.

Inspect the host's neighbour table:

```bash
ip neigh show dev br-b
```

Expected output:

```text
10.20.0.2 lladdr 8e:b6:26:91:2f:70 REACHABLE
```

The host creates a new Ethernet frame:

```text
Ethernet source:      br-b MAC
Ethernet destination: pod-b eth0 MAC

IP source:            10.10.0.2
IP destination:       10.20.0.2
```

The Layer 2 addresses changed because the packet entered a different Ethernet network.

The Layer 3 addresses stayed the same.

That is the key idea behind routing:

```text
The Ethernet frame changes at each network boundary.

The IP packet continues toward the original destination.
```

## Step 7: Give pod-a internet access

`pod-a` can now reach another private network.

Can it reach the internet too?

It already has a default route:

```text
default via 10.10.0.1 dev eth0
```

The Linux host should also have its own default route, normally through its external interface.

Find it:

```bash
ip route show default
```

Example output:

```text
default via 192.168.1.1 dev eth0 proto dhcp src 192.168.1.50
```

In this example:

```text
External interface: eth0
Host IP:             192.168.1.50
Upstream gateway:    192.168.1.1
```

Your interface might be called `ens3`, `enp0s3`, `eth0`, or something else.

Store its name so we can reuse it in the next commands:

```bash
EXT_IF=$(ip route show default | awk '/default/ {print $5; exit}')
echo "$EXT_IF"
```

Example output:

```text
eth0
```

This assumes the host has one usable default route.

If the command returns nothing or selects the wrong interface, inspect the output of `ip route show default` and set it manually:

```bash
EXT_IF=eth0
```

Use the actual external interface name from your system.

Now try to reach a public IP address from `pod-a`:

```bash
sudo ip netns exec pod-a ping -c 3 1.1.1.1
```

In a typical environment, this will still fail:

```text
3 packets transmitted, 0 received, 100% packet loss
```

We have a route.

IP forwarding is enabled.

The host knows how to send traffic toward the internet.

So what is missing?

## Step 8: Routing also needs a return path

Follow the outbound packet.

Inside `pod-a`, it starts like this:

```text
Source IP:      10.10.0.2
Destination IP: 1.1.1.1
```

The packet follows the namespace's default route to `10.10.0.1`.

The Linux host receives it, checks its own default route, and sends it through the external interface:

```text
pod-a
  |
br-a
  |
Linux host
  |
external interface
  |
internet
```

The outbound direction looks fine.

The problem is the source address:

```text
10.10.0.2
```

That address belongs to the private network we created inside the Linux host.

The outside network does not know that `10.10.0.0/24` exists behind this machine.

Even if the request reaches its destination, the reply still needs a route back to `10.10.0.2`.

It probably does not have one.

Networking is not only about getting the packet to the destination. The return path must work too.

We can observe the packet leaving with its original source address.

Start a capture on the external interface:

```bash
sudo tcpdump -n -i "$EXT_IF" 'icmp and host 1.1.1.1'
```

Then run:

```bash
sudo ip netns exec pod-a ping 1.1.1.1
```

Depending on your host firewall and upstream network, you may see:

```text
10.10.0.2 > 1.1.1.1: ICMP echo request
```

The request leaves.

The reply just has no practical route back to our private network.

## Step 9: Add NAT and masquerading

One way to solve the return-path problem is to change the packet's source address before it leaves the host.

Instead of sending this:

```text
10.10.0.2 → 1.1.1.1
```

the host can send this:

```text
192.168.1.50 → 1.1.1.1
```

`192.168.1.50` is only an example. The real value will be the IP address assigned to your external interface.

The upstream network already knows how to return traffic to that address.

This is source network address translation, usually shortened to source NAT or SNAT.

Add a masquerading rule for traffic leaving from `10.10.0.0/24`:

```bash
sudo iptables -t nat -A POSTROUTING \
  -s 10.10.0.0/24 \
  -o "$EXT_IF" \
  -j MASQUERADE
```

`MASQUERADE` is useful when the external address may change, which is common with DHCP, virtual machines, laptops, and cloud instances.

Inspect the rule:

```bash
sudo iptables -t nat -L POSTROUTING -n -v --line-numbers
```

You should see something similar to:

```text
num  pkts bytes target      source         destination
1       0     0 MASQUERADE  10.10.0.0/24  0.0.0.0/0
```

If the host's `FORWARD` policy is `DROP`, also allow outbound traffic from `br-a` and returning traffic from the external interface:

```bash
sudo iptables -I FORWARD 1 \
  -i br-a \
  -o "$EXT_IF" \
  -j ACCEPT

sudo iptables -I FORWARD 1 \
  -i "$EXT_IF" \
  -o br-a \
  -m conntrack \
  --ctstate ESTABLISHED,RELATED \
  -j ACCEPT
```

Try the ping again:

```bash
sudo ip netns exec pod-a ping -c 3 1.1.1.1
```

Expected output:

```text
64 bytes from 1.1.1.1: icmp_seq=1 ttl=56 time=12.4 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=56 time=11.9 ms
64 bytes from 1.1.1.1: icmp_seq=3 ttl=56 time=12.1 ms
```

Our tiny network can now reach the internet.

Some networks block ICMP traffic. If the routing and NAT setup looks correct but ping still fails, that does not always mean the network path is broken.

## Step 10: Watch NAT change the packet

NAT becomes much easier to understand when we observe the packet on both sides of the host.

In one terminal, listen on `br-a`:

```bash
sudo tcpdump -n -i br-a 'icmp and host 1.1.1.1'
```

In another terminal, listen on the external interface:

```bash
sudo tcpdump -n -i "$EXT_IF" 'icmp and host 1.1.1.1'
```

Run the ping again:

```bash
sudo ip netns exec pod-a ping 1.1.1.1
```

On `br-a`, you should see the original source:

```text
10.10.0.2 > 1.1.1.1: ICMP echo request
```

On the external interface, you should see the host's external address instead:

```text
192.168.1.50 > 1.1.1.1: ICMP echo request
```

The exact address will be different on your machine.

The packet changes as it crosses the host:

```text
Inside the private network:

10.10.0.2 → 1.1.1.1
      |
      | NAT on the Linux host
      v

Outside the private network:

host external IP → 1.1.1.1
```

When the reply returns, Linux reverses the translation:

```text
1.1.1.1 → host external IP
      |
      | reverse translation
      v
1.1.1.1 → 10.10.0.2
```

Linux keeps track of this state using conntrack.

The namespace does not know its source address was changed.

The remote destination does not know the packet originally came from `10.10.0.2`.

The Linux host keeps the mapping between the two.

## Three different packet paths

We have now built three related but different paths.

### Same bridge

```text
pod-a
  |
veth
  |
bridge
  |
veth
  |
pod-b
```

Both endpoints belong to the same subnet.

The bridge forwards Ethernet frames.

No gateway, IP forwarding, or NAT is required.

### Different private networks

```text
pod-a
  |
br-a
  |
Linux routing
  |
br-b
  |
pod-b
```

The endpoints belong to different subnets.

Each side needs a route through a gateway.

The Linux host forwards IP packets between the networks.

No NAT is required because both private networks have a working return path through the same router.

### Internet access

```text
pod-a
  |
br-a
  |
Linux routing
  |
NAT / masquerading
  |
external interface
  |
internet
```

The private subnet is not known to the outside network.

The Linux host translates the source address so the reply can return through an address the upstream network already knows.

| Scenario                              | Gateway needed | IP forwarding needed | NAT needed |
| ------------------------------------- | -------------: | -------------------: | ---------: |
| Same bridge and subnet                |             No |                   No |         No |
| Different private subnets             |            Yes |                  Yes |         No |
| Internet access from a private subnet |            Yes |                  Yes |    Usually |

NAT is not what makes routing happen.

Routing decides where the packet should go.

NAT changes addresses when the surrounding networks cannot route the original ones.

## How does this relate to Kubernetes nodes?

We can now return to Kubernetes with a more useful mental model.

Imagine a cluster with two nodes:

```text
Node A                                  Node B

Pod network:                            Pod network:
10.10.0.0/24                            10.20.0.0/24

pod-a: 10.10.0.2                        pod-b: 10.20.0.2
       |                                       |
 local Pod network                       local Pod network
       |                                       |
Node IP: 192.168.50.10  <---------->  Node IP: 192.168.50.11
```

`pod-a` and `pod-b` are not connected to the same local bridge.

From Node A's perspective, `10.20.0.2` belongs to the Pod network behind Node B.

Node A needs to know where that network lives.

In a small lab, we could express that using a static route:

```bash
sudo ip route add 10.20.0.0/24 via 192.168.50.11
```

Node B would need the reverse route:

```bash
sudo ip route add 10.10.0.0/24 via 192.168.50.10
```

These simplified routes assume that both node IPs belong to the same directly connected underlay network, so each node can use the other node's IP as its next hop.

Both nodes would also need to forward traffic between their Pod-facing and node-facing interfaces.

The cross-node path would look roughly like this:

```text
pod-a
  |
Node A local Pod network
  |
Node A routing table
  |
node network
  |
Node B routing table
  |
Node B local Pod network
  |
pod-b
```

The packet can preserve its original Pod addresses:

```text
Source IP:      10.10.0.2
Destination IP: 10.20.0.2
```

This is conceptually similar to the two-subnet lab we built earlier.

The scale and implementation are different, but the central question is still the same:

> How does this machine know where the destination network lives?

Static routes may be enough for a tiny lab.

A real cluster, however, needs to preserve that reachability as nodes and Pods are added, removed, or moved. Somebody has to allocate Pod addresses, create interfaces, and keep the necessary network state up to date.

That is where we will continue next.

We now understand the Linux work that needs to happen. The next question is who actually performs it when Kubernetes creates or removes a Pod.

## Cleaning everything up

Only remove firewall rules that you actually added during the article.

Delete the NAT rule:

```bash
sudo iptables -t nat -D POSTROUTING \
  -s 10.10.0.0/24 \
  -o "$EXT_IF" \
  -j MASQUERADE
```

If you added the internet forwarding rules, remove them:

```bash
sudo iptables -D FORWARD \
  -i br-a \
  -o "$EXT_IF" \
  -j ACCEPT

sudo iptables -D FORWARD \
  -i "$EXT_IF" \
  -o br-a \
  -m conntrack \
  --ctstate ESTABLISHED,RELATED \
  -j ACCEPT
```

If you added the temporary bridge-to-bridge rules, remove those too:

```bash
sudo iptables -D FORWARD -i br-a -o br-b -j ACCEPT
sudo iptables -D FORWARD -i br-b -o br-a -j ACCEPT
```

Restore IPv4 forwarding to the value you saw at the beginning.

If it was originally `0`:

```bash
sudo sysctl -w net.ipv4.ip_forward=0
```

If it was originally `1`:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
```

Delete the network namespaces:

```bash
sudo ip netns delete pod-a
sudo ip netns delete pod-b
```

Deleting the namespaces also removes their ends of the veth pairs. The host-side peers disappear with them.

Finally, delete the bridges:

```bash
sudo ip link delete br-a
sudo ip link delete br-b
```

Verify the cleanup:

```bash
ip netns list
ip link show type bridge
```

## Final thoughts

In the first article, our packet never had to leave its local Ethernet network.

This time, we gave it somewhere else to go.

We created two separate subnets, gave each namespace a gateway, enabled Linux to forward packets between them, and watched the Ethernet frame change while the IP packet kept its original source and destination.

Then we sent the same packet toward the internet and ran into a different problem: the request had a route out, but the private source network had no usable route back. NAT solved that by translating the source address into one the outside network already knew how to reach.

The important distinction is simple:

```text
Routing decides where a packet goes.

NAT changes an address when the original one cannot be routed.
```

None of this is specific to Kubernetes. But the same questions appear as soon as Pods need to communicate across nodes or leave the cluster.

We now understand the Linux side of that problem.

Next, we can look at who actually builds and maintains the Pod network when Kubernetes creates or removes a Pod.
