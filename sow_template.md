# CS 457 Project Statement of Work (SOW) & Protocol Specification Template

**Student Name:** [Austin Reynolds]  
**Date:** [2026-09-23]  
**Course:** CS 457 - Computer Networks  
**Target Server Domain:** `server.Reynolds.edu`  
**Git Repository:** [https://github.com/AustinTaylorReynolds/cs457-networked-battleship]

---

## 1. Game Selection & Scope (Sprint 0)

> Planning is going to be an iterative process through the sprints so you don't have to have all the details now. Focus on big overview concepts. You will be updating the SOW as we plan.
> You have a lot of freedom to choose a game. There are a couple caveats.  

> - It must run in the console. The lab nodes won't be able to handle extensive graphics.
> - It has to be self-contained. You can use a internet-connector to download you code, but because the architecture must run 5 nodes you won't be able to run 
> - You are encouraged to use python, but I'm not going to make it a strict requirement. The instructor and TA's ability to help with C or Rust, etc will be diminished in other languages.

### 1.1 Game Overview
- **Chosen Game:** [Battleship]
- **Player Capacity:** 2 Players (Simulated via 2 CML Client nodes)
- **Game Summary:** [Players begin the game by placing their ships onto a grid. The ships are 1 1x5, 1 1x4, 2 1x3, and 1 1x2 in number and size. The grid is 10x10 in size. Players then take turns declairing points on a grid. If the opponent has a ship placed on that section of the grid a hit is declaired. If no ship is present, miss is declaired. If a placed battleship has all of it's sections identified by the opposing player, the ship is then 'sunk'. Players alternate turns until a player has all of their ships in a sunk state. The first player to have all their battleships sunk loses the game, and the other player wins the game.]

### 1.2 Core Game Rules & Win/Draw Conditions
- **Turn Mechanics:** [Turns are enforced between players, as only one player can declaire a grid at a time. First player is chosen randomly and order is alternating.]
- **Victory Condition:** [If a placed battleship has all of it's sections identified by the opposing player, the ship is then 'sunk'. The first player to have all their battleships sunk loses the game, and the other player wins the game.]
- **Draw/Tie Condition:** [A tie can only happen if a player disconnects from the game before a victor is declaired.]

---

## 2. Application-Layer Messaging Protocol Blueprint (Sprint 1 Deliverable)

### 2.1 Message Transport & Serialization Format
- **Transport Protocol:** TCP
- **Serialization Format:** [JSON / Fixed-Header Binary / Delimited Text]
- **Framing Rule Requirement:** Explicitly define the boundary delimitation rule (e.g., newline `\n` delimiter or 4-byte big-endian length prefix) to handle TCP byte stream boundary processing. Provide raw wire stream examples and formal schema specifications for your chosen format.

#### Framing Examples:
- **Option A (Newline-Delimited JSON):**
  - *Wire Stream Example:* `{"msg_type":"CONNECT","player_id":"Alice","timestamp":1727000000}\n{"msg_type":"MOVE","player_id":"Alice","payload":{"row":0,"col":2},"timestamp":1727000005}\n`
  - *JSON Schema Definition:*
    ```json
    {
      "msg_type": "MOVE",
      "player_id": "Player_1",
      "payload": {
        "row": 0,
        "col": 2
      },
      "timestamp": 1727000000
    }
    ```
- **Option B (Text-Delimited Protocol):**
  - *Wire Stream Example:* `CONNECT|Alice|1727000000\nMOVE|Alice|0,2|1727000005\n`
  - *Grammar / Schema Definition:* `MOVE|<PLAYER_ID>|<ROW>,<COL>|<TIMESTAMP>\n`

### 2.2 Message Schema Definitions

#### Message Types:
1. `CONNECT` (Client -> Server): Request to join the game room.
2. `LOBBY_WAIT` (Server -> Client): Notification that server is waiting for Player 2.
3. `GAME_START` (Server -> Clients): Game initiated, assigns roles (e.g. Player X vs Player O).
4. `MOVE` (Client -> Server): Player action (e.g., cell coordinates or answer choice).
5. `STATE_UPDATE` (Server -> Clients): Broadcast current game board / state and active player turn.
6. `GAME_OVER` (Server -> Clients): Victory / Draw notification with final scores.
7. `ERROR` (Server -> Client): Invalid move or malformed packet error.

---

### 2.3 Game State Machine (FSM) Design (Sprint 1 Deliverable)
- **State Transitions:** Detail state flow: `INIT` -> `WAITING_FOR_PLAYERS` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN_DRAW` -> `GAME_OVER` -> `CLEANUP`.

---

## 3. Game Behavior & Server Concurrency Architecture (Sprint 2 Deliverable)

### 3.1 Server Concurrency Strategy
- **Architecture Choice:** [Multi-Threading (`threading.Thread`) OR Non-blocking I/O multiplexing (`select.select` / `selectors`)]
- **Synchronization Logic:** Explain how shared game state and client list are thread-safe (e.g. `threading.Lock`) to prevent race conditions during turn processing.

### 3.2 State & Score Synchronization Across Clients
- **Turn Enforcement:** Detail how the server validates active player ID before processing moves and broadcasts updated turn notifications to all clients.
- **Score & Board Synchronization:** Describe how state broadcasts keep client screens synchronized in real time.

---

## 4. Coding & AI Implementation Plan (Sprint 3)

- **Permitted AI Tools:** [e.g., GitHub Copilot, ChatGPT, Claude]
- **AI Prompting & Constraint Strategy:** Explain how you will constrain AI models to generate code (in Python or your chosen language) that adheres strictly to the protocol blueprint and FSM designed in Sprints 1 & 2.
- **Implementation Risk Management:** Detail your plan to leverage past programming experience and manage time to ensure code completion on schedule.

---

## 5. CML Multi-Subnet Topology & Wireshark Deployment Plan (Sprint 4 & 5 Deliverable)

> For now you can use the topology below. We may update this when we get to defining subnets.

### 5.1 Subnet & Router Design
- **Subnet A (Client 1):** `192.168.10.0/24` (Interface `Gi0/1` on Router R1)
- **Subnet B (Client 2):** `192.168.11.0/24` (Interface `Gi0/2` on Router R1)
- **Subnet C (Game Server):** `192.168.20.0/24` (Interface `Gi0/1` on Router R2)
- **Router Backbone:** `10.0.0.0/30` (Interface `Gi0/0` on R1 <-> `Gi0/0` on R2)

### 5.2 DHCP Pools & DNS Configuration Plan
- **Router R1 DHCP Pool 1 (`CLIENT1_POOL`):** Leases `192.168.10.10` - `192.168.10.50`, gateway `192.168.10.1`, DNS `10.0.0.2`.
- **Router R1 DHCP Pool 2 (`CLIENT2_POOL`):** Leases `192.168.11.10` - `192.168.11.50`, gateway `192.168.11.1`, DNS `10.0.0.2`.
- **Router R2 Authoritative DNS:** Configured with `ip dns server` and static host mapping `server.[yourlastname].edu` -> `192.168.20.100`.

### 5.3 Deployment Strategy & Wireshark Trace Capture
- **CML Deployment Strategy:** Deploy `server.py` onto Subnet C node (`192.168.20.100`) behind Router R2, and `client.py` onto Subnet A and Subnet B nodes behind Router R1.
- **Cisco Infrastructure Configuration:** Router R1 DHCP pools (`CLIENT1_POOL`, `CLIENT2_POOL`) and Router R2 authoritative DNS (`ip host server.[lastname].edu 192.168.20.100`).
- **Wireshark Trace Capture Plan:** Capture DHCP DORA exchange (`dhcp_negotiation.pcap`) and DNS query/response resolution (`dns_lookup.pcap`).
