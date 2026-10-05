# Application Protocol Blueprint

## 1. Transport Layer & Packet Framing
* **Transport Protocol:** TCP[cite: 1]
* **Serialization Format:** Structured JSON encoded in UTF-8[cite: 1, 2].
* **Framing Mechanism (Option B):** Length-Prefixed Framing (Fixed-Width Binary Header). Every message is preceded by a 4-byte unsigned integer in Network Byte Order (Big-Endian) that specifies the exact byte length of the JSON payload that follows[cite: 2].

### 1.1 TCP Stream Boundary Handling
To solve TCP stream fragmentation and coalescing, the receiver executes a deterministic extraction process:
1. Read exactly 4 bytes from the socket to determine the payload length `N`[cite: 2].
2. Enter a loop, accumulating bytes until exactly `N` bytes are received before deserializing[cite: 3].

**Wire Stream Example (Continuous Stream):**
`[4-Byte Length: 0x00000045 (69 bytes)] {"msg_type": "CONNECT", "player_id": "Austin", "timestamp": 1727000000} [4-Byte Length: 0x0000005C (92 bytes)] {"msg_type": "MOVE", "player_id": "Austin", "payload": {"row": 0, "col": 2}, "timestamp": 1727000005}`[cite: 3]

---

## 2. Message Schemas

### 2.1 CONNECT
* **Direction:** Client -> Server
* **Purpose:** Client requests to join the game room.
* **Schema & Sample:**
  ```json
  {
    "msg_type": "CONNECT",
    "player_id": "Player_1",
    "timestamp": 1727000000
  }
  ```
### 2.2 GAME_START
* **Direction:** Server -> Clients
* **Purpose:** Notifies both clients that the game has started and assigns roles.
* **Schema & Sample:**
  ```json
  {
  "msg_type": "GAME_START",
  "assigned_role": "Player_1",
  "message": "Game starting. You are Player 1."
  }
  ```
### 2.3 MOVE
* **Direction:** Client -> Server
* **Purpose:** Active player submits Battleship attack coordinates.
* **Schema & Sample:**
```json
{
    "msg_type": "MOVE",
    "player_id": "Player_1",
    "payload":{
    "row": 4,
    "col": 7
    },
    "timestamp": 1727000005
}
```
### 2.4 STATE_UPDATE
* **Direction:** Server -> Clients[cite: 4]
* **Purpose:** Broadcasts updated board state (hits/misses) and active player turn [cite:4].
* **Schema & Sample:**
```json
{
  "msg_type": "STATE_UPDATE",
  "active_turn": "Player_2",
  "last_move_result": "HIT",
  "player_1_score": 1,
  "player_2_score": 0
}
```
### 2.5 ERROR
* **Direction:** Server -> Client
* **Purpose:** Notifies client of invalid coordinates or out-of-turn actions without crashing the server.
* **Schema & Sample:**
```json
{
  "msg_type": "ERROR",
  "error_code": "INVALID_MOVE",
  "message": "Coordinates out of bounds."
}
```
### 2.6 DISCONNECT
* **Direction:** Client -> Server
* **Purpose:** Client notifies server of intentional departure to forfeit the game cleanly.
* **Schema & Sample:**
```json
{
  "msg_type": "DISCONNECT",
  "player_id": "Player_1",
  "reason": "QUIT"
}
```
### 2.7 GAME_OVER
* **Direction:** Server -> Clients
* **Purpose:** Broadcasts final game outcome (Winner/Forfeit) and final scores.
* **Schema & Sample:**
```json
{
  "msg_type": "GAME_OVER",
  "winner": "Player_2",
  "reason": "ALL_SHIPS_SUNK"
}
```
## 3. Connection Termination & Lifecycle Management

### 3.1 Graceful Application Disconnection
Clients intent on leaving send a structured `DISCONNECT` message. Following this, the application calls `sock.close()`, which initiates the TCP 4-way FIN handshake. The server reads this clean exit, declares a win by forfeit, and reclaims resources.

### 3.2 TCP 0-Byte EOF Detection
When a remote peer closes the socket cleanly, `recv()` does not raise an exception; it returns 0 bytes (`b""` in Python). To prevent infinite CPU loops, the server's receive loop explicitly checks `if not data:` to detect this End-Of-File (EOF) condition and triggers the disconnection state transition.

### 3.3 Abrupt Network Drops (TCP RST)
If a client crashes or the network drops abruptly, no FIN handshake is completed. Attempting to read or write to this severed connection raises low-level exceptions. The server's socket loops wrap operations in a `try/except` block to catch `ConnectionResetError` (TCP RST) and `BrokenPipeError` (EPIPE), triggering a cleanup transition to prevent the server process from crashing.