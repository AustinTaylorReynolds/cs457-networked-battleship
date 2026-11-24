# CS 457 Sprint 4: Network Verification Report

**Student:** Austin Reynolds
**Project:** Networked Battleship
**Server FQDN:** `server.reynolds.edu` → `192.168.20.100`
**Repository:** https://github.com/AustinTaylorReynolds/cs457-networked-battleship
**Date:** <!-- TODO: date you ran the lab -->

Deliverables in this repository:

| Deliverable | File |
|-------------|------|
| CML topology export | [`sprint_4_topology.yaml`](sprint_4_topology.yaml) |
| Wireshark DNS capture | [`sprint_4_dns_lookup.pcap`](sprint_4_dns_lookup.pcap) |
| This report | `sprint_4_network_report.md` |

Procedure followed: [`docs/sprint_4_runbook.md`](docs/sprint_4_runbook.md).

---

## 1. Topology

A single Cisco IOSv router, **ar-R1**, connects three directly attached /24 subnets. Because every subnet is a Connected route on R1, no static or dynamic routing protocol is needed. R1 is also the DHCP server for both client subnets, the authoritative DNS server for `server.reynolds.edu`, and a PAT gateway to the CML External Connector for package installs.

| Subnet | Network | R1 interface / gateway | Host | Addressing |
|--------|---------|------------------------|------|------------|
| A | `192.168.10.0/24` | Gi0/1 `192.168.10.1` | ar-Client1 | DHCP `CLIENT1_POOL` |
| B | `192.168.11.0/24` | Gi0/2 `192.168.11.1` | ar-Client2 | DHCP `CLIENT2_POOL` |
| C | `192.168.20.0/24` | Gi0/3 `192.168.20.1` | ar-Server | Static `192.168.20.100/24` |
| Outside | `192.168.255.0/24` | Gi0/0 (DHCP, NAT outside) | ext-conn | CML NAT |

### 1.1 CML topology screenshot

<!-- TODO: screenshot of the running lab in CML showing the node labels ar-Client1, ar-Client2, ar-Server, ar-R1 and ext-conn. Save as docs/sprint_4_screenshots/topology.png -->
![CML topology](docs/sprint_4_screenshots/topology.png)

---

## 2. Router R1 configuration

<!-- TODO: paste `show running-config` from ar-R1 (at least the interface, dhcp, dns, nat and access-list sections) -->
```cisco

```

---

## 3. Router R1 verification

### 3.1 `show ip interface brief`

Expected: Gi0/0 has a `192.168.255.x` lease; Gi0/1–Gi0/3 have `.10.1`, `.11.1`, `.20.1`; all four are `up up`.

<!-- TODO: paste output -->
```text

```

### 3.2 `show ip route`

Expected: `C` routes for `192.168.10.0/24`, `192.168.11.0/24`, `192.168.20.0/24` and `192.168.255.0/24`, plus `S* 0.0.0.0/0` via `192.168.255.1` from the Gi0/0 DHCP lease. No manual `ip route` statements are needed because all three internal subnets are directly connected.

<!-- TODO: paste output -->
```text

```

### 3.3 `show ip dhcp binding`

Expected: one lease in `192.168.10.0/24` (ar-Client1) and one in `192.168.11.0/24` (ar-Client2). `.1` is never leased because of `ip dhcp excluded-address`.

<!-- TODO: paste output -->
```text

```

### 3.4 `show hosts` (DNS A record)

<!-- TODO: paste output showing server.reynolds.edu -> 192.168.20.100 -->
```text

```

---

## 4. Endpoint configuration

### 4.1 ar-Server (static)

<!-- TODO: paste `ip addr show eth0`, `ip route`, `cat /etc/resolv.conf` from ar-Server -->
```text

```

### 4.2 ar-Client1 (DHCP lease)

<!-- TODO: paste `sudo udhcpc -i eth0`, `ip addr show eth0`, `ip route`, `cat /etc/resolv.conf` -->
```text

```

### 4.3 ar-Client2 (DHCP lease)

<!-- TODO: same commands as 4.2 -->
```text

```

---

## 5. Client command logs

### 5.1 ar-Client1

<!-- TODO: paste `ping -4 -c 4 192.168.20.100` and `nslookup -type=A server.reynolds.edu` -->
```text

```

### 5.2 ar-Client2

<!-- TODO: paste the same two commands from ar-Client2 -->
```text

```

**Result:** <!-- TODO: e.g. "Both clients reached 192.168.20.100 with 0% packet loss, and both resolved server.reynolds.edu to 192.168.20.100 via their gateway's DNS server." Only write this once your output confirms it. -->

---

## 6. Wireshark DNS exchange (`sprint_4_dns_lookup.pcap`)

**Capture point:** CML link capture on <!-- TODO: e.g. ar-R1 Gi0/1 <-> ar-Client1 eth0 -->
**Command that generated the traffic:** `nslookup -type=A server.reynolds.edu` on <!-- TODO: ar-Client1 -->
**Wireshark display filter:** `dns`

### 6.1 Annotated screenshot

<!-- TODO: Wireshark screenshot with both packets labelled (packet comments or drawn callouts), with the DNS detail pane of the response expanded to show the A record. Save as docs/sprint_4_screenshots/wireshark_dns.png -->
![Annotated DNS exchange](docs/sprint_4_screenshots/wireshark_dns.png)

### 6.2 Packet breakdown

Fill this table in from the capture.

| Field | ① DNS Standard Query | ② DNS Standard Query Response |
|-------|----------------------|-------------------------------|
| Frame # | <!-- TODO --> | <!-- TODO --> |
| Source IP : port | <!-- TODO: client IP : ephemeral port --> | `192.168.10.1 : 53` <!-- confirm --> |
| Destination IP : port | `192.168.10.1 : 53` <!-- confirm --> | <!-- TODO: client IP : ephemeral port --> |
| Transport | UDP | UDP |
| Transaction ID | <!-- TODO: 0x.... --> | <!-- TODO: must match the query --> |
| Flags | <!-- TODO: e.g. 0x0100 Standard query, RD --> | <!-- TODO: e.g. 0x8580 response, AA, RD, RA, No error --> |
| Question | `server.reynolds.edu: type A, class IN` | `server.reynolds.edu: type A, class IN` |
| Answer RRs | 0 | <!-- TODO: 1 --> |
| Answer | n/a | `server.reynolds.edu: type A, class IN, addr 192.168.20.100` <!-- confirm --> |

### 6.3 Explanation

1. **Query (①).** The Alpine client reads `nameserver 192.168.10.1` from `/etc/resolv.conf`. DHCP Option 6 put it there from `dns-server 192.168.10.1` in `CLIENT1_POOL`. Because `-type=A` was given, the client sends a single DNS Standard Query for the A record of `server.reynolds.edu` to UDP port 53 on its default gateway.
2. **Response (②).** `ip dns server` makes R1 answer DNS on all of its interfaces. The name matches the static `ip host server.reynolds.edu 192.168.20.100` entry, so R1 answers from its local host table without forwarding upstream. The response echoes the same Transaction ID and question and carries one A record: `192.168.20.100`. <!-- TODO: if the Authoritative (AA) flag is set in your capture, say so here -->
3. **Significance.** The Battleship client (`client.py -i server.reynolds.edu`) runs the same lookup through `socket.gethostbyname()`, so this exchange is exactly what happens before the TCP connection to `192.168.20.100:8080` opens.

---

## 7. Acceptance checklist

- [ ] Single router R1 connects Client 1 (`192.168.10.0/24`), Client 2 (`192.168.11.0/24`) and Game Server (`192.168.20.0/24`): §3.1, §3.2
- [ ] R1 issues IP, default gateway and DNS to both clients via separate DHCP pools: §3.3, §4.2, §4.3
- [ ] Game Server has static `192.168.20.100/24` with default gateway `192.168.20.1`: §4.1
- [ ] `sprint_4_dns_lookup.pcap` and screenshots show `server.reynolds.edu` → `192.168.20.100`: §5, §6
- [ ] Both clients ping the Game Server across routed subnets without packet loss: §5
- [ ] `sprint_4_topology.yaml`, `sprint_4_dns_lookup.pcap` and `sprint_4_network_report.md` committed and the repo URL submitted on Canvas
