---
external: false
featured: true
title: "What Happens When You Create a Kubernetes Service?"
description: "Part 4 of Kubernetes Networking Under the Hood: create a ClusterIP Service on a kind cluster and follow traffic from DNS through kube-proxy iptables rules to Pod endpoints."
date: 2026-08-28
series: "Kubernetes Networking Under the Hood"
seriesPart: 4
categories:
  - Kubernetes
tags:
  - linux
  - networking
  - kubernetes
  - cka
references:
  - title: "Kubernetes Services"
    url: "https://kubernetes.io/docs/concepts/services-networking/service/"
  - title: "EndpointSlices"
    url: "https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/"
  - title: "Virtual IPs and Service Proxies"
    url: "https://kubernetes.io/docs/reference/networking/virtual-ips/"
  - title: "DNS for Services and Pods"
    url: "https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/"
  - title: "CoreDNS for Service Discovery"
    url: "https://kubernetes.io/docs/tasks/administer-cluster/coredns/"
  - title: "kind Cluster Configuration"
    url: "https://kind.sigs.k8s.io/docs/user/configuration/"
---

Hey, long time no see.

In the first three parts, we built the Pod network from the bottom up.

We started with network namespaces, veth pairs, and a Linux bridge. Then we moved traffic between different networks with routes and forwarding. Finally, we stopped configuring those pieces ourselves and let CNI plugins do it for us.

At this point, a Pod can get an IP address and communicate with other Pods.

So let us use that network.

Suppose we have an application called `payments` running with three replicas.

Each replica gets its own Pod IP:

```text
payments-...   10.244.1.2
payments-...   10.244.1.4
payments-...   10.244.1.6
```

The exact addresses will change between runs, but that is part of the point.

Another Pod can connect directly to any of them.

The problem is that those addresses belong to individual Pods, not to the application.

If a Pod disappears, its replacement may receive a different IP.

The network still works.

We just no longer know which address represents `payments`.

In this article, we will create a Kubernetes Service and look at what Kubernetes builds around it:

```text
Service
   |
   +--> ClusterIP
   |
   +--> EndpointSlice
   |
   +--> kube-proxy dataplane rules
   |
   +--> DNS name
```

Then we will follow a request from:

```text
http://payments
```

to the Pod that eventually receives it.

## What are we building?

For this lab, I am using a two-node `kind` cluster:

```text
control-plane
worker
```

with:

```text
Pod network:      10.244.0.0/16
Service network:  10.96.0.0/16
kube-proxy mode:  iptables
```

I explicitly use kube-proxy's `iptables` mode so we can inspect the Service dataplane directly.

Create `kind.yaml`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4

networking:
  podSubnet: "10.244.0.0/16"
  serviceSubnet: "10.96.0.0/16"
  kubeProxyMode: "iptables"

nodes:
  - role: control-plane
  - role: worker
```

Create the cluster:

```bash
kind create cluster \
  --name services-lab \
  --config kind.yaml
```

Check the nodes:

```bash
kubectl get nodes
```

You should see:

```text
services-lab-control-plane   Ready
services-lab-worker          Ready
```

The exact Kubernetes version depends on the `kind` node image.

Pod placement can also differ between runs.

In my lab, all three `payments` replicas ended up on the worker node. That is perfectly fine for what we are testing here. The Service abstraction does not depend on the scheduler placing replicas on different nodes.

If a selected backend lives on another node in a larger cluster, the cross-node Pod networking we looked at earlier is what carries the packet there.

## Step 1: Create the application

We need a backend that tells us which Pod answered each request.

A small BusyBox HTTP server is enough.

Create `payments.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
spec:
  replicas: 3
  selector:
    matchLabels:
      app: payments
  template:
    metadata:
      labels:
        app: payments
    spec:
      containers:
        - name: payments
          image: busybox:1.36.1
          command:
            - sh
            - -c
            - |
              mkdir -p /www
              hostname > /www/index.html
              httpd -f -p 8080 -h /www
          ports:
            - containerPort: 8080
```

Apply it:

```bash
kubectl apply -f payments.yaml
```

Wait for the replicas:

```bash
kubectl rollout status deployment/payments
```

Then inspect them:

```bash
kubectl get pods \
  -l app=payments \
  -o wide
```

In my lab, the Pods received addresses from the worker's Pod subnet. During the experiment, the current set included addresses such as:

```text
10.244.1.2
10.244.1.4
10.244.1.6
```

Your addresses and Pod names will be different.

These are ordinary Pod IPs provided by the Pod network.

## Step 2: Connect directly to a Pod

Create a client:

```bash
kubectl run client \
  --image=busybox:1.36.1 \
  --restart=Never \
  -- sleep 3600
```

Wait until it is ready:

```bash
kubectl wait \
  --for=condition=Ready pod/client \
  --timeout=60s
```

Pick one of the `payments` Pod IPs:

```bash
kubectl get pods \
  -l app=payments \
  -o wide
```

Then connect directly to it:

```bash
kubectl exec client -- \
  wget -qO- http://10.244.1.3:8080
```

In my lab, the response was the hostname of that Pod:

```text
payments-84d79bb576-...
```

So direct Pod-to-Pod communication works.

But the client now knows:

```text
10.244.1.3
```

That address belongs to one particular replica.

Delete it:

```bash
kubectl delete pod <pod-name>
```

The Deployment creates a replacement.

In my run, the old backend:

```text
10.244.1.3
```

was eventually replaced by a new Pod using:

```text
10.244.1.6
```

The application still has three healthy replicas.

But `10.244.1.3` is no longer a useful address for a client.

This is not a Pod networking problem.

The network is working.

The problem is that a changing set of Pods needs a stable way to be reached.

## Step 3: Create a Service

Create `payments-service.yaml`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: payments
spec:
  selector:
    app: payments
  ports:
    - name: http
      port: 80
      targetPort: 8080
```

Apply it:

```bash
kubectl apply -f payments-service.yaml
```

Inspect the Service:

```bash
kubectl get service payments
```

In my lab:

```text
NAME       TYPE        CLUSTER-IP      PORT(S)
payments   ClusterIP   10.96.129.45    80/TCP
```

We created a Kubernetes object and got another IP address:

```text
10.96.129.45
```

Where did that come from?

## The ClusterIP

We did not specify a Service type, so Kubernetes created a `ClusterIP` Service.

A ClusterIP is a virtual address used to reach the Service from inside the cluster.

Our cluster has this Service address range:

```text
10.96.0.0/16
```

Kubernetes allocated:

```text
10.96.129.45
```

from that range for our Service.

We now have two different types of addresses:

```text
Pod network

10.244.1.x


Service network

10.96.129.45
```

The Pod addresses identify individual endpoints.

The ClusterIP identifies the Service.

They solve different problems.

## Step 4: What did the selector do?

Our Service contains:

```yaml
selector:
  app: payments
```

The Deployment gives the same label to its Pods:

```yaml
labels:
  app: payments
```

Kubernetes uses that relationship to determine which Pods belong behind the Service.

We can see that state through EndpointSlices.

Run:

```bash
kubectl get endpointslice \
  -l kubernetes.io/service-name=payments \
  -o wide
```

Or print only the endpoint addresses:

```bash
kubectl get endpointslice \
  -l kubernetes.io/service-name=payments \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"\n"}{end}'
```

The addresses matched the current `payments` Pod IPs in my lab.

Conceptually:

```text
             Service
             payments
          10.96.129.45
                 |
                 v
          EndpointSlice
                 |
        +--------+--------+
        |        |        |
        v        v        v
   Pod IP     Pod IP     Pod IP
```

There is an important separation here.

The Service gives us a stable frontend:

```text
10.96.129.45
```

The EndpointSlice represents the current backend set.

When Pods change, the EndpointSlice can change while the Service IP stays the same.

## Step 5: Replace a backend

We already saw that a replacement Pod can receive another address.

Now that the Service exists, we can see what happens to its backend state.

Check the current endpoints:

```bash
kubectl get endpointslice \
  -l kubernetes.io/service-name=payments \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"\n"}{end}'
```

Delete one of the backend Pods:

```bash
kubectl delete pod <pod-name>
```

Wait for the Deployment to restore the replica count:

```bash
kubectl rollout status deployment/payments
```

Then inspect the EndpointSlice again.

The old Pod address disappears and the replacement address appears.

Now check the Service:

```bash
kubectl get service payments
```

In my lab, its ClusterIP remained:

```text
10.96.129.45
```

So we have:

```text
Pod changed
     |
     v
EndpointSlice changed

Service IP stayed the same
```

That gives clients a stable destination even though the backend set changes.

But there is another question.

What actually owns `10.96.129.45`?

## Step 6: Is the ClusterIP on an interface?

We already know what an ordinary Linux IP address looks like.

If an interface owns an address, `ip addr` can show it.

Store the Service IP:

```bash
SERVICE_IP=$(kubectl get svc payments \
  -o jsonpath='{.spec.clusterIP}')

echo "$SERVICE_IP"
```

Output in my lab:

```text
10.96.129.45
```

Now inspect a node:

```bash
docker exec services-lab-control-plane \
  ip addr
```

Search specifically for the ClusterIP:

```bash
docker exec services-lab-control-plane \
  ip addr | grep -F "$SERVICE_IP"
```

There was no output in my lab.

No normal interface on that node owns:

```text
10.96.129.45
```

Yet we can send packets to it.

The ClusterIP is a **virtual IP**.

Something on the node has to recognize traffic for that destination and map it to one of the current backend Pods.

That is where kube-proxy fits.

## What kube-proxy is doing

Each node runs kube-proxy unless the cluster uses another implementation for Kubernetes Services.

kube-proxy watches Services and EndpointSlices and keeps the node's Service dataplane synchronized with that Kubernetes state.

In this lab, kube-proxy uses the Linux iptables API.

It is important not to picture kube-proxy as a process sitting between applications and copying traffic from one socket to another.

In iptables mode, kube-proxy programs kernel packet-processing rules.

The actual packet forwarding is done by the kernel.

The control path looks roughly like this:

```text
API server
    |
    | Service + EndpointSlice state
    v
kube-proxy
    |
    | program rules
    v
Linux netfilter / iptables
```

The packet path is different:

```text
client packet
     |
     | dst = 10.96.129.45:80
     v
Linux dataplane
     |
     | select backend
     v
10.244.1.x:8080
```

kube-proxy prepares the dataplane.

The packet itself travels through the kernel.

## Step 7: Find the Service in iptables

kube-proxy programs Service rules on every node.

We only need to inspect one of them, so I will use the control-plane node.

Search the NAT table for our Service IP:

```bash
docker exec services-lab-control-plane \
  iptables-save -t nat | grep -F "$SERVICE_IP"
```

The Service appeared in `KUBE-SERVICES` in my lab.

The generated rule follows this shape:

```text
-A KUBE-SERVICES ... \
  -d 10.96.129.45/32 \
  -p tcp ... --dport 80 \
  -j KUBE-SVC-...
```

The exact generated chain name will differ.

Conceptually:

```text
traffic to
10.96.129.45:80
        |
        v
KUBE-SERVICES
        |
        v
KUBE-SVC-...
```

We can inspect the generated Service and endpoint chains:

```bash
docker exec services-lab-control-plane \
  iptables-save -t nat | \
  grep -E 'KUBE-SVC|KUBE-SEP|payments'
```

The output contains other Kubernetes rules as well, but the path for our Service follows this structure:

```text
KUBE-SERVICES
      |
      | match Service IP + port
      v
KUBE-SVC-...
      |
      | choose endpoint
      v
KUBE-SEP-...
      |
      | DNAT
      v
Pod IP:8080
```

The `KUBE-SVC-*` chain represents the Service.

The `KUBE-SEP-*` chains represent its individual endpoints.

In my lab, the endpoint chains contained DNAT targets such as:

```text
10.244.1.6:8080
10.244.1.7:8080
```

along with the other current backend.

In iptables mode, kube-proxy creates rules for the available endpoints and uses probabilistic rules by default to distribute new connections between them.

We do not need to follow every probability calculation here.

The important part is that the Service chain eventually reaches an endpoint chain.

That chain performs destination NAT, or DNAT.

So a packet originally addressed to:

```text
10.96.129.45:80
```

can be rewritten toward something like:

```text
10.244.1.6:8080
```

Nothing needs to be listening on `10.96.129.45` as a normal application socket.

The virtual IP works because the node dataplane recognizes it.

## Service, EndpointSlice, and kube-proxy

It helps to separate their responsibilities.

```text
Service
   |
   | stable frontend
   v
10.96.129.45:80


EndpointSlice
   |
   | current backends
   v
10.244.1.x:8080


kube-proxy
   |
   | programs node dataplane
   v
Service IP -> backend endpoint
```

The Service itself does not forward packets.

The EndpointSlice does not forward packets either.

They are Kubernetes API state.

kube-proxy watches that state and programs the node dataplane to make the Service behave like a reachable virtual destination.

### When the backend set changes

Earlier, we replaced a Pod and saw its address change in the EndpointSlice.

kube-proxy watches those EndpointSlice updates and keeps the node rules synchronized with the current backend set.

So the relationship is:

```text
Pod replaced
     |
     v
EndpointSlice updated
     |
     v
kube-proxy updates node rules
     |
     v
Service IP stays the same
```

The client does not need to track those Pod addresses itself.

## Step 8: Send traffic through the Service

Call the ClusterIP from the client:

```bash
kubectl exec client -- \
  wget -qO- "http://${SERVICE_IP}"
```

One of the backend Pods responds.

Run several new requests:

```bash
for i in $(seq 1 10); do
  kubectl exec client -- \
    wget -qO- "http://${SERVICE_IP}"
done
```

In my lab, those ten requests reached two different replicas.

The exact distribution is not important.

What matters is the path:

```text
client
   |
   | dst 10.96.129.45:80
   v
Service dataplane
   |
   | select endpoint
   | DNAT
   v
10.244.1.x:8080
   |
   v
payments Pod
```

And this is where the previous articles come back.

Once the destination has become a Pod IP, the Service layer has done its part.

Getting the packet to that Pod is a Pod-networking problem again.

```text
Service VIP
    |
    | Service dataplane
    v
Pod IP
    |
    | CNI-provided Pod network
    | routing when another node is involved
    v
destination Pod
```

A Service did not replace the network from Parts 1, 2, and 3.

It added another layer on top of it.

## Step 9: Finding the Service by name

The client can now use a stable ClusterIP instead of tracking individual Pod addresses.

But applications usually do not put ClusterIPs into their configuration either.

Kubernetes Services also get DNS names.

To verify the record directly, use the full Service name:

```bash
kubectl exec client -- \
  nslookup payments.default.svc.cluster.local
```

In my lab, it resolved to:

```text
Name:      payments.default.svc.cluster.local
Address:   10.96.129.45
```

Now call it using only the short Service name:

```bash
kubectl exec client -- \
  wget -qO- http://payments
```

That worked as well.

So the request can start with:

```text
payments
    |
    | DNS
    v
10.96.129.45
    |
    | Service dataplane
    v
10.244.1.x
    |
    | Pod network
    v
payments Pod
```

One small lab detail: BusyBox `nslookup payments` can print the correct Service address while still returning a non-zero exit code as it tries additional search-domain candidates.

Using the full name for the explicit DNS check avoids making that behavior part of the experiment.

The application itself can still use the short name `payments`.

## How does Kubernetes resolve the Service name?

Look at the DNS configuration inside the client:

```bash
kubectl exec client -- cat /etc/resolv.conf
```

It contains a cluster DNS nameserver and search domains similar to:

```text
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local
options ndots:5
```

The exact nameserver address can differ between clusters.

The `nameserver` points at the cluster DNS Service.

The search domains are why a Pod in the `default` namespace can use:

```text
payments
```

instead of the full Service name:

```text
payments.default.svc.cluster.local
```

In this `kind` cluster, CoreDNS provides cluster DNS.

The discovery path is therefore:

```text
application
     |
     | payments
     v
CoreDNS
     |
     | Service name -> ClusterIP
     v
10.96.129.45
```

DNS tells us **which Service IP to use**.

The Service dataplane determines **which backend receives the connection**.

The Pod network determines **how that packet reaches the backend Pod**.

Those are separate jobs.

## Putting the whole path together

We started this series below Kubernetes.

In Part 1:

```text
namespace
    |
   veth
    |
 bridge
```

In Part 2:

```text
Pod network
    |
 routing
    |
other network
```

In Part 3:

```text
runtime
    |
   CNI
    |
plugins
    |
Linux networking
```

Now we can put the application-facing layer on top:

```text
curl http://payments
          |
          | DNS
          v
payments Service
10.96.129.45:80
          |
          | kube-proxy programmed dataplane
          v
selected EndpointSlice backend
10.244.1.x:8080
          |
          | CNI-provided Pod network
          | routing if another node is involved
          v
destination Pod
```

The responsibilities line up like this:

```text
DNS
 |
 | "Which Service?"
 v

Service
 |
 | "What stable destination represents it?"
 v

EndpointSlice
 |
 | "Which Pods currently back it?"
 v

kube-proxy / Service dataplane
 |
 | "Which endpoint receives this connection?"
 v

CNI + Linux networking
 |
 | "How does the packet reach that Pod?"
 v

Pod
```

This is the picture I wanted to reach when we started with two network namespaces and a bridge.

Kubernetes networking becomes easier to reason about once these pieces stop looking like one abstraction doing everything.

They are connected, but they solve different problems.

## Cleaning up

Delete the Service and application:

```bash
kubectl delete -f payments-service.yaml
kubectl delete -f payments.yaml
kubectl delete pod client
```

Then remove the lab cluster:

```bash
kind delete cluster --name services-lab
```

## Final thoughts

A Service is easy to create.

A few lines of YAML give us a stable destination for a changing set of Pods.

Behind that object, several pieces work together.

The ClusterIP gives the Service a stable virtual address.

The selector connects the Service to its current backends through EndpointSlices.

kube-proxy watches that state and programs the node dataplane so traffic for the virtual address can reach one of those endpoints.

Cluster DNS gives the Service a stable name so applications do not need to know the virtual IP either.

And once the Service dataplane chooses a Pod, we are back in the networking from the previous articles: Pod IPs, CNI, routes, interfaces, and Linux moving the packet to its destination.

Across the four parts, the path now looks like this:

```text
name
  |
 DNS
  |
Service
  |
Service dataplane
  |
Pod IP
  |
CNI
  |
Linux networking
  |
Pod
```

When I started this series, most of these pieces were familiar on their own.

I knew what a Service was. I had used CNI-backed clusters, looked at Pod IPs, debugged DNS, and worked with Kubernetes networking before.

What I wanted was to stop seeing them as separate Kubernetes concepts and understand how they actually connect.

Starting with two Linux network namespaces and following the packet upward turned out to be a useful way to do that.

The bridge from Part 1 is still there underneath the abstractions. So are the routes from Part 2 and the Linux objects created through CNI in Part 3. A Service does not replace any of them. It gives applications a stable way to use the network they already provide.

At least for the traffic inside the cluster, the picture now feels a lot less magical.

And that was really the point of this series.
