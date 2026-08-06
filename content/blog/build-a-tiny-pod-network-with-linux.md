---
external: false
featured: true
title: "Build a Tiny Pod Network with Linux"
description: "Part 1 of Kubernetes Networking Under the Hood: build a small Pod-like network with Linux network namespaces, veth pairs, and a bridge—no cluster, CNI, or container runtime required."
date: 2026-08-07
series: "Kubernetes Networking Under the Hood"
seriesPart: 1
categories:
  - Kubernetes
tags:
  - linux
  - networking
  - cni
  - cka
references:
  - title: "Linux network namespaces"
    url: "https://man7.org/linux/man-pages/man7/network_namespaces.7.html"
  - title: "Virtual Ethernet devices"
    url: "https://man7.org/linux/man-pages/man4/veth.4.html"
  - title: "Linux kernel bridge documentation"
    url: "https://docs.kernel.org/networking/bridge.html"
  - title: "The CNI specification"
    url: "https://github.com/containernetworking/cni/blob/main/SPEC.md"
  - title: "Kubernetes cluster networking"
    url: "https://kubernetes.io/docs/concepts/cluster-administration/networking/"
---

Not long ago, I decided to go back to the fundamentals of Kubernetes and started preparing for the CKA certification.

I have been using Kubernetes for quite a while, but networking was always one of those areas I mostly understood through the abstractions Kubernetes provides. I knew enough to use it and debug common problems, but I never built a particularly strong mental model of what was happening underneath.

Preparing for the CKA gave me a good reason to slow down and look at that layer more closely. Part of the reason I had not done that before is simply how work tends to go. Between deadlines, incidents, meetings, and the next thing that needs to be shipped, fundamentals are often the first thing we stop making time for.

Abstractions exist for a reason, and nobody can understand every layer of every system they use. The problem begins when the abstraction becomes our only mental model. When something breaks outside the happy path, our understanding may end exactly where that abstraction does.

Okay, that is enough philosophy for an article about virtual Ethernet devices. Let us get into it!

In this article, we are going to build a small Pod-like network by hand using Linux network namespaces, virtual Ethernet devices, and a bridge.

There will be no Kubernetes cluster, container runtime, or CNI plugin involved.

For now, the goal is to build the smallest useful version of the idea, follow a packet through it, and make what happens under the hood a little less mysterious. From there, we can gradually work our way toward cross-node communication and the larger Kubernetes networking model built on top of these Linux primitives.

## What are we building?

By the end of the article, we will have two isolated network environments communicating through a Linux bridge.

Our final setup will look like this:

```text
                         Linux host

                    br0: 10.10.0.1
                     /           \
                    /             \
         veth-a-host               veth-b-host
              |                         |
              |                         |
             eth0                      eth0
        10.10.0.2                 10.10.0.3
   pod-a network namespace   pod-b network namespace
```

The two network namespaces will act as our fake Pods.

Each one will have:

- its own network interfaces
- its own IP addresses
- its own routing table
- its own neighbour table
- its own isolated view of the network stack

They will be connected to a shared Linux bridge through virtual Ethernet pairs.

These are not real Kubernetes Pods, of course. Still, the Linux primitives we are going to use are closely related to the ones used by container networking.

## Before starting

You need a Linux environment with root access.

I am using commands from the `iproute2` package. On Ubuntu or Debian, you can install the tools we need with:

```bash
sudo apt update
sudo apt install iproute2 iputils-ping
```

The `bridge` command we will use later is also included in the `iproute2` package.

We will use `tcpdump` to watch the traffic:

```bash
sudo apt install tcpdump
```

This setup will not work directly on macOS because network namespaces are a Linux feature. If you are using a Mac, the easiest option is to run the commands inside a Linux virtual machine.

## Step 1: Create two isolated network worlds

Let us start by creating two network namespaces:

```bash
sudo ip netns add pod-a
sudo ip netns add pod-b
```

A network namespace gives processes an isolated view of the network stack.

We can list the network namespaces we just created:

```bash
ip netns list
```

The output should look similar to this:

```text
pod-b
pod-a
```

Now let us inspect the interfaces inside the `pod-a` network namespace:

```bash
sudo ip netns exec pod-a ip link
```

You may also see the shorter form:

```bash
sudo ip -n pod-a link
```

It does the same thing here: runs the `ip link` command inside the `pod-a` network namespace. I will keep using `ip netns exec` for now because it makes the namespace boundary a little more explicit.

You should see only the loopback interface:

```text
1: lo: <LOOPBACK> mtu 65536 state DOWN mode DEFAULT
```

The network namespace exists, but it is completely isolated.

It does not have a useful network interface, an IP address, or a route to anywhere.

Our current topology is basically this:

```text
pod-a network namespace        pod-b network namespace

           lo                             lo
         DOWN                           DOWN
```

We have created two separate network worlds. Now we need a way to connect them.

## Step 2: Create a virtual cable

We will begin with `pod-a`.

To connect a network namespace to something outside itself, we can use a virtual Ethernet pair, usually called a veth pair.

A veth pair consists of two connected interfaces. A packet entering one side appears on the other.

Create the pair:

```bash
sudo ip link add veth-a-host type veth peer name veth-a
```

We now have two connected interfaces:

```text
veth-a-host <------------> veth-a
```

Both currently live in the host network namespace.

We want one side to remain on the host and the other side to belong to the `pod-a` network namespace.

Move `veth-a` into it:

```bash
sudo ip link set veth-a netns pod-a
```

Now the two ends of the pair live in different network namespaces:

```text
Host network namespace             pod-a network namespace

veth-a-host  <----------------->           veth-a
```

We can verify both sides.

On the host:

```bash
ip link show veth-a-host
```

Inside `pod-a`:

```bash
sudo ip netns exec pod-a ip link
```

The output inside the network namespace should now contain both `lo` and `veth-a`:

```text
1: lo: <LOOPBACK> mtu 65536 state DOWN
2: veth-a@if3: <BROADCAST,MULTICAST> mtu 1500 state DOWN
```

The interface numbers and the value after `@if` will probably be different on your machine.

At this point, `pod-a` has a connection to the host network namespace.

It still cannot communicate with anything, though. Both interfaces are down, and the host side is not connected to a network.

## Step 3: Why do we need a bridge?

We could connect two network namespaces directly with a veth pair.

That might work for exactly two network namespaces, but it does not scale particularly well.

Imagine having five or ten of them. Connecting every network namespace directly to every other network namespace would quickly become a mess.

Instead, we can connect the host side of each veth pair to a shared bridge.

A Linux bridge behaves like a small virtual Layer 2 switch. It learns which MAC addresses are reachable through which ports and forwards Ethernet frames accordingly.

Create the bridge:

```bash
sudo ip link add br0 type bridge
```

One detail that can feel slightly confusing at first is that the bridge itself also appears as a network interface on the host.

That is why we can bring `br0` up and assign an IP address to it just like we would with another interface. Its bridge ports, such as `veth-a-host`, are then attached underneath it.

Bring it up:

```bash
sudo ip link set br0 up
```

We will also assign an IP address to it:

```bash
sudo ip addr add 10.10.0.1/24 dev br0
```

The bridge does not need an IP address just to forward Ethernet frames between the network namespaces.

We are giving it one so the host can also communicate with them. It could also become their gateway later if we decided to add connectivity outside this small network.

Now attach the host side of our veth pair to the bridge:

```bash
sudo ip link set veth-a-host master br0
sudo ip link set veth-a-host up
```

Our topology now looks like this:

```text
Host network namespace             pod-a network namespace

           br0
            |
      veth-a-host  <------------->         veth-a
```

We can verify that the interface belongs to the bridge:

```bash
bridge link
```

The output should contain `veth-a-host` with `master br0`.

A simplified version looks like this:

```text
3: veth-a-host@if2: <BROADCAST,MULTICAST,UP,LOWER_UP>
    master br0 state forwarding
```

The important part is:

```text
master br0
```

That tells us `veth-a-host` is now one of the bridge ports.

### So we found another abstraction

At this point, after all that talk about looking underneath abstractions, it is probably worth admitting that we have already landed on another set of abstractions.

A veth pair gives us the behaviour of an Ethernet cable without an actual cable. A Linux bridge gives us the basic behaviour of a network switch without a physical switch sitting on the desk.

In a physical setup, we might connect two machines to a switch using real cables.

In our setup, the machines are network namespaces, the cables are veth pairs, and the switch is a Linux bridge.

So yes, we removed the Kubernetes abstraction only to find more abstractions underneath it.

Apparently, there is no final layer where everything suddenly stops being an abstraction and becomes obvious. There are just smaller pieces that are easier to inspect.

The good news is that we do not need to crawl under a desk and untangle any cables for this one.

## Step 4: Finish configuring pod-a

The network namespace has an interface, but the interface is still called `veth-a`, has no IP address, and is down.

Inside a container, the primary network interface is normally called `eth0`.

Let us rename it:

```bash
sudo ip netns exec pod-a ip link set veth-a name eth0
```

Now assign an IP address:

```bash
sudo ip netns exec pod-a ip addr add 10.10.0.2/24 dev eth0
```

Bring the interface and loopback device up:

```bash
sudo ip netns exec pod-a ip link set eth0 up
sudo ip netns exec pod-a ip link set lo up
```

Instead of checking the result after every command, we can now inspect the final state:

```bash
sudo ip netns exec pod-a ip addr
```

The relevant part of the output should look similar to this:

```text
1: lo: <LOOPBACK,UP,LOWER_UP>
    inet 127.0.0.1/8

2: eth0@if3: <BROADCAST,MULTICAST,UP,LOWER_UP>
    inet 10.10.0.2/24
```

Linux also created a route for the connected subnet automatically:

```bash
sudo ip netns exec pod-a ip route
```

Expected output:

```text
10.10.0.0/24 dev eth0 proto kernel scope link src 10.10.0.2
```

This route means that addresses in the `10.10.0.0/24` network are directly reachable through `eth0`.

We can now test communication between `pod-a` and the bridge:

```bash
sudo ip netns exec pod-a ping -c 3 10.10.0.1
```

You should receive replies:

```text
64 bytes from 10.10.0.1: icmp_seq=1 ttl=64 time=0.050 ms
64 bytes from 10.10.0.1: icmp_seq=2 ttl=64 time=0.048 ms
64 bytes from 10.10.0.1: icmp_seq=3 ttl=64 time=0.047 ms
```

We now have one isolated network namespace connected to the host through a veth pair and a Linux bridge.

```text
                         Host network namespace

                              br0
                          10.10.0.1
                              |
                        veth-a-host
                              |
                              |
                             eth0
                          10.10.0.2
                   pod-a network namespace
```

## Step 5: Add pod-b

The `pod-b` network namespace needs the same pieces:

- a veth pair
- one end moved into the network namespace
- the host end attached to the bridge
- an IP address from the same subnet

We already walked through each operation for `pod-a`, so we can configure `pod-b` in one go:

```bash
sudo ip link add veth-b-host type veth peer name veth-b

sudo ip link set veth-b netns pod-b

sudo ip link set veth-b-host master br0
sudo ip link set veth-b-host up

sudo ip netns exec pod-b ip link set veth-b name eth0
sudo ip netns exec pod-b ip addr add 10.10.0.3/24 dev eth0
sudo ip netns exec pod-b ip link set eth0 up
sudo ip netns exec pod-b ip link set lo up
```

Inspect the final state:

```bash
sudo ip netns exec pod-b ip addr
```

You should see:

```text
1: lo: <LOOPBACK,UP,LOWER_UP>
    inet 127.0.0.1/8

2: eth0@if5: <BROADCAST,MULTICAST,UP,LOWER_UP>
    inet 10.10.0.3/24
```

Our tiny network is now complete:

```text
                         Linux host

                    br0: 10.10.0.1
                     /           \
                    /             \
         veth-a-host               veth-b-host
              |                         |
              |                         |
             eth0                      eth0
        10.10.0.2                 10.10.0.3
   pod-a network namespace   pod-b network namespace
```

## Step 6: Test communication

Let us send traffic from `pod-a` to `pod-b`:

```bash
sudo ip netns exec pod-a ping -c 3 10.10.0.3
```

Expected output:

```text
64 bytes from 10.10.0.3: icmp_seq=1 ttl=64 time=0.080 ms
64 bytes from 10.10.0.3: icmp_seq=2 ttl=64 time=0.063 ms
64 bytes from 10.10.0.3: icmp_seq=3 ttl=64 time=0.067 ms
```

Try the opposite direction too:

```bash
sudo ip netns exec pod-b ping -c 3 10.10.0.2
```

### If the ping does not work

If you do not receive a reply, check the final state before changing the setup:

```bash
sudo ip -n pod-a addr
sudo ip -n pod-b addr

sudo ip -n pod-a link
sudo ip -n pod-b link

sudo ip -n pod-a route
sudo ip -n pod-b route

bridge link
```

Make sure that:

- both addresses include the `/24` prefix
- both `eth0` interfaces are up
- both host-side veth interfaces are up and attached to `br0`
- both network namespaces have a connected route for `10.10.0.0/24`

Host firewall or nftables rules can also interfere with bridged traffic on some systems. If the interfaces, addresses, and routes all look correct, inspect the host firewall rules next.

I would not recommend disabling the firewall blindly, especially outside a disposable learning environment.

The two isolated network namespaces can now communicate with each other.

There is no Kubernetes cluster here.

There is no container runtime.

There is no CNI plugin.

Linux is moving the packets.

The ping works, but the more interesting question is this:

> What path did the packet actually take?

## Step 7: Follow one packet

When `pod-a` sends a packet to `10.10.0.3`, the packet follows this path:

```text
pod-a eth0
    |
    v
veth-a-host
    |
    v
   br0
    |
    v
veth-b-host
    |
    v
pod-b eth0
```

Let us walk through it.

### pod-a checks its routing table

Before sending anything, `pod-a` needs to decide where the packet should go.

Its routing table contains:

```text
10.10.0.0/24 dev eth0 proto kernel scope link src 10.10.0.2
```

The destination `10.10.0.3` belongs to the same subnet, so `pod-a` knows it can reach the destination directly through `eth0`.

No gateway is needed.

### pod-a needs a MAC address

The IP address tells `pod-a` which destination it wants to reach, but Ethernet delivery requires a destination MAC address.

If `pod-a` does not already know the MAC address for `10.10.0.3`, it sends an ARP request:

```text
Who has 10.10.0.3?
Tell 10.10.0.2.
```

That ARP frame leaves through `eth0`.

### The veth pair moves the frame to the host

`eth0` inside `pod-a` is one side of a veth pair.

When the frame enters `eth0`, it appears on `veth-a-host` in the host network namespace.

The veth pair itself does not make routing decisions. It simply carries traffic from one end to the other.

### The bridge forwards the frame

`veth-a-host` is connected to `br0`.

The bridge receives the Ethernet frame and forwards it through its ports.

Because an ARP request is a broadcast, the bridge sends it through the other relevant ports, including `veth-b-host`.

### The second veth pair delivers it to pod-b

The frame enters `veth-b-host` and appears on `eth0` inside the `pod-b` network namespace.

`pod-b` sees that the ARP request is asking about its IP address and replies with its MAC address.

After that exchange, `pod-a` can send the ICMP packet directly to `pod-b`.

We can inspect the neighbour table inside `pod-a`:

```bash
sudo ip netns exec pod-a ip neigh
```

You should see an entry similar to:

```text
10.10.0.3 dev eth0 lladdr 8a:41:36:d5:92:c4 REACHABLE
```

The MAC address will be different on your machine.

The neighbour table is where Linux keeps the relationship between IP addresses and link-layer addresses learned through protocols such as ARP.

## Step 8: What has the bridge learned?

Like a physical Ethernet switch, a Linux bridge learns which MAC addresses are reachable through which ports.

Because we have already generated traffic between the network namespaces, the bridge should now have learned their MAC addresses.

We can inspect its forwarding database:

```bash
bridge fdb show br br0
```

The output will contain entries associated with `veth-a-host` and `veth-b-host`.

A simplified example might look like this:

```text
8a:41:36:d5:92:c4 dev veth-b-host master br0
d2:72:f4:e8:51:10 dev veth-a-host master br0
```

This tells the bridge which port it should use when forwarding frames to each MAC address.

The bridge did not need us to configure those MAC addresses manually. It learned them by observing the source addresses of frames passing through it.

If you run this command before generating any traffic, you may not see the same learned entries yet.

## Step 9: Watch the traffic yourself

Diagrams and explanations are useful, but we do not have to trust them.

We can watch the packets moving through the network.

Start `tcpdump` on the bridge:

```bash
sudo tcpdump -n -i br0
```

In another terminal, run:

```bash
sudo ip netns exec pod-a ping 10.10.0.3
```

You should see ICMP traffic:

```text
10.10.0.2 > 10.10.0.3: ICMP echo request
10.10.0.3 > 10.10.0.2: ICMP echo reply
```

If you clear the neighbour entry first:

```bash
sudo ip netns exec pod-a ip neigh flush all
```

and start the ping again, you should also see the ARP exchange:

```text
ARP, Request who-has 10.10.0.3 tell 10.10.0.2
ARP, Reply 10.10.0.3 is-at 8a:41:36:d5:92:c4
```

You can observe the same traffic at different points in the path.

Inside `pod-a`:

```bash
sudo ip netns exec pod-a tcpdump -n -i eth0
```

On the host side of its veth pair:

```bash
sudo tcpdump -n -i veth-a-host
```

On the bridge:

```bash
sudo tcpdump -n -i br0
```

This becomes useful when debugging real container networking.

If you can see a packet inside the Pod network namespace but not on the host-side veth interface, the problem is somewhere near that boundary.

If you can see it on the bridge but not on the destination interface, the problem is further along the path.

Networking debugging often comes down to following the packet and finding the point where it disappears.

## So where does Kubernetes fit?

When Kubernetes creates a Pod, it does not normally run these exact shell commands itself.

A simplified Pod creation flow looks like this:

```text
Scheduler selects a node
          |
          v
Kubelet asks the container runtime
to create a Pod sandbox
          |
          v
A network namespace is prepared
          |
          v
The configured CNI plugin is called
          |
          v
Interfaces, addresses, and routes
are configured using Linux
```

The scheduler decides which node should run the Pod.

The kubelet on that node asks the container runtime to create the Pod sandbox.

The runtime and CNI integration then handle the network setup.

It helps to think of CNI as a contract rather than a particular networking technology. CNI defines how a runtime invokes networking plugins and how those plugins report the result. It does not require every plugin to build the network in the same way.

What we have done manually is roughly the kind of work a CNI plugin may automate:

- create a veth pair
- move one end into the Pod network namespace
- rename it to `eth0`
- assign the Pod IP address
- configure routes
- connect the host side to a bridge or another networking layer
- bring the interfaces up
- configure routing, encapsulation, policy, or eBPF programs when needed

The runtime invokes the configured plugin when a Pod is added to or removed from the network. The actual network design is left to the plugin.

A simple bridge-based plugin might create something close to what we built.

Other plugins work differently.

- **Calico** may rely heavily on routing and BGP.
- **Flannel** may use VXLAN to create an overlay network.
- Cloud CNI plugins may assign addresses from the cloud virtual network directly to Pods.
- **Cilium** can use eBPF for networking, load balancing, and policy enforcement.

So it would not be correct to say:

```text
Kubernetes networking is a Linux bridge.
```

A better way to put it is:

```text
Kubernetes networking is built using Linux networking primitives,
configured according to the model implemented by the CNI plugin.
```

Kubernetes defines the desired model.

The CNI plugin configures it.

Linux carries the packets.

## What this tiny network does not cover

Our setup only demonstrates communication between two Pod-like environments on the same Linux machine.

A real Kubernetes network needs to solve several additional problems.

For example, what happens if `pod-a` runs on Node 1 and `pod-b` runs on Node 2?

```text
Node 1                              Node 2

pod-a: 10.10.1.2                    pod-b: 10.10.2.3
```

Node 1 needs to know how to reach the Pod network on Node 2.

That might involve:

- normal Linux routes
- BGP route distribution
- VXLAN encapsulation
- IP-in-IP
- cloud network routes
- eBPF-based forwarding

We have also not covered:

- Kubernetes Services
- ClusterIP addresses
- kube-proxy
- NAT
- DNS
- NetworkPolicy
- ingress traffic
- internet access
- conntrack
- cross-node MTU problems

Our bridge solves the same-node part of the problem: two Pod-like network environments can reach each other directly on one Linux host.

The next layer is cluster-wide reachability. A Pod on one node must be able to reach a Pod on another node while preserving the Pod addressing model.

That is where Linux routing, overlays, VXLAN, BGP, cloud routes, and different CNI implementations begin to matter.

Before following a packet across nodes, though, it helps to understand how it leaves the Pod in the first place. That is the part we have built here.

## Cleaning everything up

When you are finished, delete the network namespaces:

```bash
sudo ip netns delete pod-a
sudo ip netns delete pod-b
```

Deleting the network namespaces also deletes the interfaces inside them.

When one side of a veth pair is deleted, the other side disappears as well.

Finally, remove the bridge:

```bash
sudo ip link delete br0
```

You can verify that everything is gone:

```bash
ip netns list
ip link show
```

## Final thoughts

In this article, we manually created:

- two isolated network namespaces
- two virtual Ethernet pairs
- one Linux bridge
- two Pod-like IP addresses
- a working network path between them

We also followed a packet from one network namespace to the other:

```text
pod-a eth0
    |
veth-a-host
    |
   br0
    |
veth-b-host
    |
pod-b eth0
```

This is not a complete Kubernetes network, but it gives us a useful mental model.

A Pod is not magically connected to the network.

It has a network namespace.

It has a virtual interface.

That interface connects to something on the node.

The node then needs to decide where the packet goes next.

Once those pieces become visible, Kubernetes networking starts to feel less like a collection of magic abstractions and more like automated Linux networking.

The next question is what happens when the destination Pod is not on the same bridge, or even on the same node.
