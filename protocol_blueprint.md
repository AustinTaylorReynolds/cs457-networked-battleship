# Application Protocol Blueprint (v2, Sprint 2 revision)

## 1. Transport Layer & Packet Framing
* **Transport Protocol:** TCP
* **Serialization Format:** Structured JSON encoded in UTF-8.
* **Framing Mechanism (Option B):** Length-Prefixed Framing. Every message is preceded by a 4-byte unsigned integer in Network Byte Order (Big-Endian, `struct` format `!I`) giving the exact byte length of the JSON payload that follows.

### 1.1 TCP Stream Boundary Handling
1. Read exactly 4 bytes (via `recv_exact(sock, 4)`) to get the payload length `N`.
2. Loop, accumulating chunks until exactly `N` bytes are received, then decode and `json.loads`.

**Wire Stream Example (continuous stream):**
`[4-Byte Length: 0x00000045 (69 bytes)] {"msg_type": "CONNECT", "player_id": "Austin", "timestamp": 1727000000} [4-Byte Length: 0x0000005C (92 bytes)] {"msg_type": "MOVE", "player_id": "Player_1", "payload": {"row": 0, "col": 2}, "timestamp": 1727000005}`

### 1.2 Identity Convention
* `CONNECT.player_id` is a display name chosen by the user (e.g. `"Austin"`).
* After the server replies with `JOINED`, the client uses the `assigned_role` (`"Player_1"` or `"Player_2"`) as `player_id` in every later message.

### 1.3 Ship Fleet and Board
* Board: 10x10, rows and columns 0-9.
* Fleet (5 ships): `CARRIER` (5), `BATTLESHIP` (4), `CRUISER` (3), `SUBMARINE` (3), `DESTROYER` (2).
* Ships are straight lines (`H` = extends toward increasing col, `V` = extends toward increasing row), must be fully in bounds, and must not overlap.

---

## 2. Message Schemas

| # | `msg_type` | Direction | Purpose |
|---|---|---|---|
| 1 | `CONNECT` | Client -> Server | Request to join the game room |
| 2 | `JOINED` | Server -> Client | Acknowledges join and assigns a role |
| 3 | `LOBBY_WAIT` | Server -> Client | Waiting for the second player |
| 4 | `GAME_START` | Server -> Clients | Both players present; begin ship placement |
| 5 | `PLACE_SHIPS` | Client -> Server | Submit the full fleet layout |
| 6 | `PLACEMENT_OK` | Server -> Client | Fleet accepted; reports whether opponent is ready |
| 7 | `BATTLE_START` | Server -> Clients | Both fleets placed; names the first active player |
| 8 | `MOVE` | Client -> Server | Active player fires at a coordinate |
| 9 | `STATE_UPDATE` | Server -> Clients | Result of a shot, scores, and next turn |
| 10 | `GAME_OVER` | Server -> Clients | Winner, reason, final scores |
| 11 | `ERROR` | Server -> Client | Invalid, out-of-turn, or malformed request |
| 12 | `DISCONNECT` | Client -> Server | Intentional departure (forfeit) |

### 2.1 CONNECT
```json
{ "msg_type": "CONNECT", "player_id": "Austin", "timestamp": 1727000000 }
```

### 2.2 JOINED
```json
{ "msg_type": "JOINED", "assigned_role": "Player_1", "message": "Joined as Player 1." }
```

### 2.3 LOBBY_WAIT
Sent only to the first player while the second has not connected.
```json
{ "msg_type": "LOBBY_WAIT", "message": "Waiting for Player 2 to join..." }
```

### 2.4 GAME_START
```json
{
  "msg_type": "GAME_START",
  "assigned_role": "Player_1",
  "opponent_name": "Sam",
  "message": "Game starting. Place your ships."
}
```

### 2.5 PLACE_SHIPS
```json
{
  "msg_type": "PLACE_SHIPS",
  "player_id": "Player_1",
  "payload": {
    "ships": [
      { "name": "CARRIER",    "row": 0, "col": 0, "orientation": "H" },
      { "name": "BATTLESHIP", "row": 2, "col": 1, "orientation": "V" },
      { "name": "CRUISER",    "row": 5, "col": 5, "orientation": "H" },
      { "name": "SUBMARINE",  "row": 8, "col": 0, "orientation": "H" },
      { "name": "DESTROYER",  "row": 9, "col": 7, "orientation": "H" }
    ]
  },
  "timestamp": 1727000003
}
```

### 2.6 PLACEMENT_OK
```json
{ "msg_type": "PLACEMENT_OK", "opponent_ready": false, "message": "Fleet accepted. Waiting for opponent." }
```

### 2.7 BATTLE_START
First player is chosen randomly by the server.
```json
{ "msg_type": "BATTLE_START", "active_turn": "Player_2", "message": "All ships placed. Player 2 fires first." }
```

### 2.8 MOVE
```json
{
  "msg_type": "MOVE",
  "player_id": "Player_2",
  "payload": { "row": 4, "col": 7 },
  "timestamp": 1727000005
}
```

### 2.9 STATE_UPDATE
Broadcast to **both** clients. The shooter learns the result; the defender learns which of their cells was hit.
```json
{
  "msg_type": "STATE_UPDATE",
  "shooter": "Player_2",
  "row": 4,
  "col": 7,
  "last_move_result": "SUNK",
  "sunk_ship": "DESTROYER",
  "active_turn": "Player_1",
  "hits": { "Player_1": 3, "Player_2": 6 },
  "ships_remaining": { "Player_1": 5, "Player_2": 4 },
  "wins": { "Player_1": 0, "Player_2": 1 }
}
```
* `last_move_result`: `HIT`, `MISS`, or `SUNK`.
* `sunk_ship`: ship name when `SUNK`, otherwise `null`.
* `hits`: hits **scored by** each player this round. `wins`: session wins.
* `ships_remaining`: ships each player still has afloat.

### 2.10 GAME_OVER
```json
{
  "msg_type": "GAME_OVER",
  "winner": "Player_2",
  "reason": "ALL_SHIPS_SUNK",
  "final_hits": { "Player_1": 11, "Player_2": 17 },
  "wins": { "Player_1": 0, "Player_2": 1 }
}
```
* `reason`: `ALL_SHIPS_SUNK` or `FORFEIT`.

### 2.11 ERROR
```json
{ "msg_type": "ERROR", "error_code": "NOT_YOUR_TURN", "message": "It is not your turn." }
```

| `error_code` | Meaning |
|---|---|
| `INVALID_MOVE` | Row/col outside 0-9, or cell already fired on |
| `NOT_YOUR_TURN` | MOVE from the non-active player |
| `INVALID_PLACEMENT` | Fleet out of bounds, overlapping, wrong ship set, or already submitted |
| `WRONG_PHASE` | Message not allowed in the current FSM state |
| `MALFORMED_MESSAGE` | Bad JSON, unknown `msg_type`, or missing fields |
| `SERVER_FULL` | A third client tried to connect; connection is then closed |

### 2.12 DISCONNECT
```json
{ "msg_type": "DISCONNECT", "player_id": "Player_1", "reason": "QUIT" }
```

---

## 3. Connection Termination & Lifecycle Management

### 3.1 Graceful Application Disconnection
A client leaving sends `DISCONNECT`, then calls `sock.close()` (TCP FIN handshake). The server declares a **win by forfeit** for the remaining player and sends `GAME_OVER` with `reason: "FORFEIT"`.

### 3.2 TCP 0-Byte EOF Detection
A clean remote close makes `recv()` return `b""`. `recv_exact` raises/returns on EOF, and the handler treats it as a disconnect.

### 3.3 Abrupt Network Drops (TCP RST)
Socket operations are wrapped in `try/except` for `ConnectionResetError` and `BrokenPipeError`, which trigger the same forfeit transition without crashing the server.

### 3.4 Disconnect Outcome Rule
A disconnect during `PLACING_SHIPS` or battle is always a **forfeit win** for the remaining player. (This replaces the "tie" wording in the Sprint 0 SOW; there is no draw in Battleship.)
