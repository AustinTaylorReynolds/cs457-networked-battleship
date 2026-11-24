# Sprint 4 Runbook: Deploy and Verify the CML Network

This is the step-by-step procedure for producing the three Sprint 4 deliverables.
Every command block is labelled with the node it runs on.

| Node label   | Role                 | Address                          | Interface on R1 |
|--------------|----------------------|----------------------------------|-----------------|
| `ar-R1`      | Gateway, DHCP, DNS, NAT | Gi0/1 `.10.1`, Gi0/2 `.11.1`, Gi0/3 `.20.1`, Gi0/0 DHCP | n/a |
| `ar-Client1` | Battleship client    | DHCP from `CLIENT1_POOL` (expect `192.168.10.2`) | Gi0/1 |
| `ar-Client2` | Battleship client    | DHCP from `CLIENT2_POOL` (expect `192.168.11.2`) | Gi0/2 |
| `ar-Server`  | Battleship server    | Static `192.168.20.100/24`, gw `192.168.20.1` | Gi0/3 |
| `ext-conn`   | External Connector (NAT) | `192.168.255.0/24`           | Gi0/0 |

FQDN: `server.reynolds.edu` resolves to `192.168.20.100` (A record on R1).

---

## 1. Import and start the lab

1. In CML, go to **Dashboard → Import** and upload `sprint_4_topology.yaml`.
2. Open the lab. The canvas should show `ar-R1` in the middle, `ar-Client1` / `ar-Client2` above it, `ar-Server` below it, and `ext-conn` to the left.
3. **Start Lab**. R1 (IOSv) takes 2-4 minutes to boot. The Alpine nodes come up much faster.

Each node's **Config** tab already holds its full configuration, so it is applied at first boot:

* **ar-R1**: every command from Sections 2.1, 2.3, 2.4 and 2.5 of the assignment.
* **ar-Server**: the static IP, default route and `/etc/resolv.conf` from Section 2.2.
* **ar-Client1 / ar-Client2**: `udhcpc -i eth0 -b`, which keeps retrying until R1 is up.

> If you edit a node's Config tab after it has already booted once, you must **Stop → Wipe** that node before the new config takes effect.

Alpine login is `cisco` / `cisco`.

## 2. Verify R1 (screenshots or copied text for the report)

```cisco
! ar-R1 console
show ip interface brief
show ip route
show ip dhcp binding
show hosts
show ip nat translations
```

What to look for:

* `show ip interface brief`: Gi0/0 has a `192.168.255.x` address; Gi0/1, Gi0/2 and Gi0/3 show `.10.1`, `.11.1` and `.20.1`. All four are `up / up`.
* `show ip route`: three `C` routes (`192.168.10.0/24`, `192.168.11.0/24`, `192.168.20.0/24`), plus `C 192.168.255.0/24` and an `S*` default route via `192.168.255.1` learned from the DHCP lease on Gi0/0.
* `show ip dhcp binding`: two leases, one in `192.168.10.0/24` and one in `192.168.11.0/24`. If it's empty, the clients have not leased yet (see step 3).
* `show hosts`: `server.reynolds.edu` with `192.168.20.100`.

The startup config is already in NVRAM after first boot, but run `write memory` anyway after any manual change.

## 3. Verify the clients (Section 2.3)

```bash
# ar-Client1 and ar-Client2 consoles
sudo ip link set eth0 up
sudo udhcpc -i eth0          # re-run by hand so the lease shows in your console log
ip addr show eth0
ip route
cat /etc/resolv.conf
```

Expected values:

* ar-Client1: `inet 192.168.10.2/24`, `default via 192.168.10.1`, `nameserver 192.168.10.1`
* ar-Client2: `inet 192.168.11.2/24`, `default via 192.168.11.1`, `nameserver 192.168.11.1`

## 4. Verify the server (Section 2.2)

```bash
# ar-Server console
ip addr show eth0        # inet 192.168.20.100/24
ip route                 # default via 192.168.20.1
cat /etc/resolv.conf     # nameserver 192.168.20.1
```

If the boot script did not run, apply it by hand:

```bash
sudo ip addr add 192.168.20.100/24 dev eth0
sudo ip link set eth0 up
sudo ip route add default via 192.168.20.1
echo "nameserver 192.168.20.1" | sudo tee /etc/resolv.conf
```

## 5. Required client command logs

Run on **both** clients and save the output for the report:

```bash
ping -4 -c 4 192.168.20.100
nslookup -type=A server.reynolds.edu
ping -4 -c 4 server.reynolds.edu     # optional: proves name -> IP -> reachability end to end
```

Expect `4 packets transmitted, 4 packets received, 0% packet loss` and an nslookup answer of `Address: 192.168.20.100` from server `192.168.10.1` (Client 1) or `192.168.11.1` (Client 2).

Use `-4` and `-type=A`. Without them Alpine also asks for an AAAA record, which R1 does not have, and you get `bad address` / `Can't find ...: No answer`.

## 6. DNS packet capture → `sprint_4_dns_lookup.pcap` (Section 3.1)

1. In the CML canvas, click the link **ar-R1 Gi0/1 ↔ ar-Client1 eth0**.
2. In the link's sidebar open **Packet Capture** and click **Start**. Leave the filter blank (or use `udp port 53`).
3. On **ar-Client1** run:
   ```bash
   nslookup -type=A server.reynolds.edu
   ```
4. Back in CML, click **Stop**, then **Download** the capture.
5. Rename the downloaded file to exactly `sprint_4_dns_lookup.pcap` and put it in the repository root.
6. Open it in Wireshark with the display filter `dns` and confirm two packets:
   * **Standard query** `A server.reynolds.edu`, `192.168.10.2 → 192.168.10.1`, UDP dst port 53.
   * **Standard query response** with `server.reynolds.edu: type A, class IN, addr 192.168.20.100`, `192.168.10.1 → 192.168.10.2`, UDP src port 53, same Transaction ID as the query.

**Annotate for the report.** In Wireshark, right-click each packet and choose **Packet Comment...** to label it "DNS Standard Query (UDP 53)" and "DNS Standard Response → 192.168.20.100". Then take a screenshot with the packet list and the expanded *Domain Name System* detail pane visible. If you want the comments saved inside the file, save as pcapng. The deliverable must still be named `.pcap`, so keep the raw CML download as `sprint_4_dns_lookup.pcap` and keep the annotation in the report screenshot.

## 7. (Optional) Play Battleship across the subnets

This isn't graded in Sprint 4, but it proves the whole stack and sets up Sprint 5. Section 2.5's NAT gives the nodes Internet access for `apk`:

```bash
# every Alpine node
sudo apk update
sudo apk add git python3
git clone https://github.com/AustinTaylorReynolds/cs457-networked-battleship.git
sync

# ar-Server
cd cs457-networked-battleship && python3 server.py -p 8080

# ar-Client1 and ar-Client2
cd cs457-networked-battleship && python3 client.py -i server.reynolds.edu -p 8080
```

`client.py` resolves the name with `socket.gethostbyname()`, which is IPv4/A-record only, so the AAAA caveat above does not apply.

## 8. Export the final topology

The rubric wants an **exported** CML topology, so after everything verifies:

1. On R1: `write memory`.
2. In CML, choose **Lab → Extract Configs**. This pulls R1's running config into the node's Config tab.
3. Choose **Lab → Download Lab** (or Export) and save it over `sprint_4_topology.yaml` in the repo root.

## 9. Commit and submit

```bash
git add sprint_4_topology.yaml sprint_4_dns_lookup.pcap sprint_4_network_report.md docs/sprint_4_screenshots/
git commit -m "Sprint 4: CML topology, DNS capture, network verification report"
git push origin main
```

Then submit `https://github.com/AustinTaylorReynolds/cs457-networked-battleship` on Canvas.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Client has no IPv4 after boot | R1 was still booting. Run `sudo udhcpc -i eth0` again. |
| Client got `192.168.10.1` | The `excluded-address` lines are missing. Check `show run \| include excluded`. |
| Ping to server times out but the server can ping R1 | The server has no default route. Check `ip route` on ar-Server for `default via 192.168.20.1`. |
| `nslookup` times out | Check `show run \| include dns server` on R1 and confirm the client's `/etc/resolv.conf`. |
| `apk update` fails | R1 Gi0/0 has no DHCP address (check `show ip int br`), or the NAT lines are missing (`show ip nat translations` stays empty). Don't add `ip name-server 8.8.8.8`; external UDP 53 is blocked on campus. |
| `bad address 'server.reynolds.edu'` | Use `ping -4`. See Section 5. |
