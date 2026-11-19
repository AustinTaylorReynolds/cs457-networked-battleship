#!/usr/bin/env python3
"""Networked Battleship server (CS 457, Sprint 3).

Implements the design documents in ``docs/``:

* ``protocol_blueprint.md``: TCP, UTF-8 JSON, 4-byte big-endian
  length-prefixed frames (struct format ``!I``).
* ``fsm_specification.md``: INIT -> WAITING_FOR_PLAYERS -> PLACING_SHIPS ->
  PLAYER_TURN -> EVALUATE_MOVE -> GAME_OVER -> CLEANUP.
* ``concurrency_design.md``: the main thread runs ``accept()``; one daemon
  handler thread per client runs a blocking receive loop. One global lock
  (``state_lock``) protects all shared game state, and every handler follows
  acquire -> validate -> mutate -> broadcast -> release.

Usage:
    python3 server.py -h
    python3 server.py -p 8080
"""

import argparse
import json
import logging
import random
import socket
import struct
import sys
import threading
import time
from enum import Enum

log = logging.getLogger("battleship.server")

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
HEADER = struct.Struct("!I")        # 4-byte unsigned, network byte order
MAX_PAYLOAD = 64 * 1024             # sanity cap on a single JSON frame
SEND_TIMEOUT = 5.0                  # seconds; bounds every sendall()
CONNECT_TIMEOUT = 30.0              # seconds a new socket may stay silent

BOARD_SIZE = 10
FLEET = {                           # ship name -> length (insertion ordered)
    "CARRIER": 5,
    "BATTLESHIP": 4,
    "CRUISER": 3,
    "SUBMARINE": 3,
    "DESTROYER": 2,
}
ROLES = ("Player_1", "Player_2")
CLIENT_MSG_TYPES = {"CONNECT", "PLACE_SHIPS", "MOVE", "DISCONNECT"}
MAX_NAME_LEN = 32


class State(Enum):
    """Server-side game FSM states (docs/fsm_specification.md)."""

    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    PLACING_SHIPS = "PLACING_SHIPS"
    PLAYER_TURN = "PLAYER_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------
class FrameError(Exception):
    """The byte stream is corrupt (bad length); the stream cannot resync."""


class MalformedMessage(Exception):
    """A complete frame arrived but its payload is not valid JSON."""


class Rejected(Exception):
    """A request failed validation; carries the ERROR code to send back."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code
        self.text = text


def recv_exact(sock, n, deadline=None):
    """Read exactly ``n`` bytes, accumulating chunks (TCP is a stream).

    Raises EOFError when the peer closes (``recv`` returns ``b""``).
    Socket timeouts are only used to bound sends, so a timeout here just
    means "still idle" and the loop keeps waiting, unless ``deadline`` (a
    ``time.monotonic()`` value) has passed.
    """
    chunks = []
    remaining = n
    while remaining:
        try:
            chunk = sock.recv(remaining)
        except socket.timeout:
            if deadline is not None and time.monotonic() > deadline:
                raise
            continue
        if not chunk:
            raise EOFError("peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock, deadline=None):
    """Read one length-prefixed JSON frame and return the decoded object."""
    (length,) = HEADER.unpack(recv_exact(sock, HEADER.size, deadline))
    if length == 0 or length > MAX_PAYLOAD:
        raise FrameError("illegal frame length %d" % length)
    payload = recv_exact(sock, length)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MalformedMessage("payload is not valid UTF-8 JSON") from exc


def send_message(sock, msg):
    """Serialize ``msg`` and write header + payload as one frame."""
    data = json.dumps(msg).encode("utf-8")
    sock.sendall(HEADER.pack(len(data)) + data)


def error_msg(code, text):
    """Build an ERROR message (protocol section 2.11)."""
    return {"msg_type": "ERROR", "error_code": code, "message": text}


def other_role(role):
    """Return the opposing role."""
    return ROLES[1] if role == ROLES[0] else ROLES[0]


def is_int(value):
    """True for real integers (``bool`` is excluded on purpose)."""
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Fleet validation (pure function, no shared state)
# ---------------------------------------------------------------------------
def build_board(ships):
    """Validate a PLACE_SHIPS fleet and return a 10x10 grid of ship names.

    Raises ``Rejected`` with MALFORMED_MESSAGE for structurally bad input or
    INVALID_PLACEMENT for rule violations (wrong set, out of bounds, overlap).
    """
    if not isinstance(ships, list):
        raise Rejected("MALFORMED_MESSAGE", "payload.ships must be a list.")
    grid = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    seen = set()
    for ship in ships:
        if not isinstance(ship, dict):
            raise Rejected("MALFORMED_MESSAGE", "Each ship must be an object.")
        name = ship.get("name")
        row, col = ship.get("row"), ship.get("col")
        orient = ship.get("orientation")
        if not isinstance(name, str) or not is_int(row) or not is_int(col):
            raise Rejected("MALFORMED_MESSAGE",
                           "Ship needs name, integer row and integer col.")
        if orient not in ("H", "V"):
            raise Rejected("MALFORMED_MESSAGE", "orientation must be H or V.")
        if name not in FLEET or name in seen:
            raise Rejected("INVALID_PLACEMENT",
                           "Unknown or duplicate ship '%s'." % name)
        seen.add(name)
        for i in range(FLEET[name]):
            r = row + (i if orient == "V" else 0)
            c = col + (i if orient == "H" else 0)
            if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
                raise Rejected("INVALID_PLACEMENT",
                               "%s is out of bounds." % name)
            if grid[r][c] is not None:
                raise Rejected("INVALID_PLACEMENT",
                               "%s overlaps %s." % (name, grid[r][c]))
            grid[r][c] = name
    if seen != set(FLEET):
        raise Rejected("INVALID_PLACEMENT",
                       "Fleet must contain exactly: " + ", ".join(FLEET))
    return grid


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class Connection:
    """One client socket. ``role`` and ``name`` are set on CONNECT."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.role = None
        self.name = None
        self.closed = False

    def label(self):
        """Short text for log lines."""
        who = "%s '%s'" % (self.role, self.name) if self.role else "new"
        return "%s@%s:%s" % (who, self.addr[0], self.addr[1])


class GameServer:
    """All shared game state lives here, guarded by ``state_lock``.

    Helpers whose names end in ``_locked`` assume the caller already holds
    ``state_lock`` and never acquire it themselves (it is not reentrant).
    """

    def __init__(self, host="0.0.0.0", port=0):
        self.host = host
        self.port = port
        self.server_sock = None
        self._stop = threading.Event()
        self.state_lock = threading.Lock()
        self.fsm_state = State.INIT
        self.session_wins = {}            # display name -> rounds won
        self._reset_round_locked()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        """Bind and listen (INIT -> WAITING_FOR_PLAYERS)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(5)
        sock.settimeout(1.0)              # lets the accept loop notice stop
        self.server_sock = sock
        self.port = sock.getsockname()[1]
        with self.state_lock:
            self.fsm_state = State.WAITING_FOR_PLAYERS
        log.info("Listening on %s:%d (state=WAITING_FOR_PLAYERS)",
                 self.host, self.port)

    def serve_forever(self):
        """Accept loop; never reads game traffic, so it never blocks on it."""
        while not self._stop.is_set():
            try:
                sock, addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break                     # listening socket was closed
            sock.settimeout(SEND_TIMEOUT)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = Connection(sock, addr)
            log.info("Connection from %s:%s", addr[0], addr[1])
            threading.Thread(target=self.client_loop, args=(conn,),
                             daemon=True).start()

    def shutdown(self):
        """Stop accepting and close every client socket."""
        self._stop.set()
        if self.server_sock:
            try:                      # wakes a blocked accept() immediately
                self.server_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                  # not supported on every OS; poll covers
            self.server_sock.close()
        with self.state_lock:
            for conn in list(self.clients.values()):
                self._close_conn(conn)

    # -- per-client thread -------------------------------------------------
    def client_loop(self, conn):
        """Blocking receive loop for one client (runs in its own thread)."""
        deadline = time.monotonic() + CONNECT_TIMEOUT
        try:
            while True:
                # Blocking reads happen OUTSIDE the lock.
                try:
                    msg = recv_message(
                        conn.sock, deadline if conn.role is None else None)
                except MalformedMessage as exc:
                    with self.state_lock:
                        self._send_locked(
                            conn, error_msg("MALFORMED_MESSAGE", str(exc)))
                    continue
                if self.handle_message(conn, msg):
                    break
        except EOFError:
            if not conn.closed:       # not a close the server itself did
                log.info("%s closed the connection (EOF)", conn.label())
        except FrameError as exc:
            log.warning("%s sent a corrupt frame: %s", conn.label(), exc)
            with self.state_lock:
                self._send_locked(
                    conn, error_msg("MALFORMED_MESSAGE", str(exc)))
        except socket.timeout:
            log.info("%s never sent CONNECT; dropping", conn.label())
        except OSError as exc:            # ConnectionReset, BrokenPipe, ...
            if not conn.closed:
                log.info("%s connection error: %s", conn.label(), exc)
        except Exception:                 # never let one client kill us
            log.exception("Unexpected error in handler for %s", conn.label())
        finally:
            self.handle_disconnect(conn)

    def handle_message(self, conn, msg):
        """Process one message. Returns True when the connection should end.

        Lock protocol: acquire, validate, mutate, broadcast, release.
        """
        with self.state_lock:                               # 1. ACQUIRE
            if conn.closed:
                return True
            try:
                mtype = msg.get("msg_type") if isinstance(msg, dict) else None
                if mtype not in CLIENT_MSG_TYPES:
                    raise Rejected("MALFORMED_MESSAGE",
                                   "Unknown or missing msg_type.")
                if mtype == "DISCONNECT":
                    log.info("%s sent DISCONNECT", conn.label())
                    return True
                parsed = self._validate_locked(conn, mtype, msg)  # 2. VALIDATE
            except Rejected as rej:
                log.info("Rejected %s from %s: %s",
                         rej.code, conn.label(), rej.text)
                self._send_locked(conn, error_msg(rej.code, rej.text))
                return rej.code == "SERVER_FULL"
            self._apply_locked(conn, mtype, parsed)   # 3. MUTATE 4. BROADCAST
            return False                                    # 5. RELEASE

    def handle_disconnect(self, conn):
        """Forfeit or clean up after a client leaves (EOF, RST, DISCONNECT)."""
        with self.state_lock:
            role = conn.role
            if conn.closed or role is None or \
                    self.clients.get(role) is not conn:
                self._close_conn(conn)    # stale, rejected, or never joined
                return
            log.info("%s disconnected in state %s",
                     conn.label(), self.fsm_state.name)
            if self.fsm_state in (State.PLACING_SHIPS, State.PLAYER_TURN,
                                  State.EVALUATE_MOVE):
                self._close_conn(conn)
                self._finish_round_locked(other_role(role), "FORFEIT")
            else:                         # lobby: no GAME_OVER, just CLEANUP
                self._cleanup_locked()

    # -- validation (no mutation) -----------------------------------------
    def _validate_locked(self, conn, mtype, msg):
        """Raise ``Rejected`` on any problem; return parsed data otherwise."""
        if mtype == "CONNECT":
            return self._validate_connect_locked(conn, msg)
        if conn.role is None:
            raise Rejected("WRONG_PHASE", "Send CONNECT first.")
        if mtype == "PLACE_SHIPS":
            return self._validate_place_locked(conn, msg)
        return self._validate_move_locked(conn, msg)

    def _check_identity(self, conn, msg):
        if msg.get("player_id") != conn.role:
            raise Rejected("MALFORMED_MESSAGE",
                           "player_id does not match your assigned role.")

    def _validate_connect_locked(self, conn, msg):
        if conn.role is not None:
            raise Rejected("WRONG_PHASE", "You have already joined.")
        if (len(self.clients) >= len(ROLES)
                or self.fsm_state != State.WAITING_FOR_PLAYERS):
            raise Rejected("SERVER_FULL", "The game room is full.")
        name = msg.get("player_id")
        if not isinstance(name, str) or not name.strip() \
                or len(name.strip()) > MAX_NAME_LEN:
            raise Rejected("MALFORMED_MESSAGE",
                           "player_id must be a 1-%d character name."
                           % MAX_NAME_LEN)
        return name.strip()

    def _validate_place_locked(self, conn, msg):
        if self.fsm_state != State.PLACING_SHIPS:
            raise Rejected("WRONG_PHASE", "Not accepting fleets right now.")
        self._check_identity(conn, msg)
        if conn.role in self.ready:
            raise Rejected("INVALID_PLACEMENT", "Fleet already submitted.")
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            raise Rejected("MALFORMED_MESSAGE", "Missing payload object.")
        return build_board(payload.get("ships"))

    def _validate_move_locked(self, conn, msg):
        # Order matches docs/concurrency_design.md section 3.3.
        if self.fsm_state != State.PLAYER_TURN:
            raise Rejected("WRONG_PHASE", "No battle is in progress.")
        self._check_identity(conn, msg)
        if conn.role != self.active_turn:
            raise Rejected("NOT_YOUR_TURN", "It is not your turn.")
        payload = msg.get("payload")
        if not isinstance(payload, dict) or not is_int(payload.get("row")) \
                or not is_int(payload.get("col")):
            raise Rejected("MALFORMED_MESSAGE",
                           "MOVE needs integer payload.row and payload.col.")
        row, col = payload["row"], payload["col"]
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            raise Rejected("INVALID_MOVE", "Coordinates must be 0-9.")
        if (row, col) in self.shots[conn.role]:
            raise Rejected("INVALID_MOVE", "You already fired at that cell.")
        return row, col

    # -- mutation + broadcast ---------------------------------------------
    def _apply_locked(self, conn, mtype, parsed):
        if mtype == "CONNECT":
            self._apply_connect_locked(conn, parsed)
        elif mtype == "PLACE_SHIPS":
            self._apply_place_locked(conn, parsed)
        else:
            self._apply_move_locked(conn, parsed)

    def _apply_connect_locked(self, conn, name):
        role = next(r for r in ROLES if r not in self.clients)
        conn.role, conn.name = role, name
        self.clients[role] = conn
        log.info("%s joined as %s", name, role)
        self._send_locked(conn, {
            "msg_type": "JOINED", "assigned_role": role,
            "message": "Joined as %s." % role.replace("_", " ")})
        if len(self.clients) < len(ROLES):
            self._send_locked(conn, {
                "msg_type": "LOBBY_WAIT",
                "message": "Waiting for Player 2 to join..."})
            return
        self.fsm_state = State.PLACING_SHIPS
        log.info("Two players present: state=PLACING_SHIPS")
        for r in ROLES:
            self._send_locked(self.clients[r], {
                "msg_type": "GAME_START", "assigned_role": r,
                "opponent_name": self.clients[other_role(r)].name,
                "message": "Game starting. Place your ships."})

    def _apply_place_locked(self, conn, grid):
        role = conn.role
        self.boards[role] = grid
        self.ships_left[role] = dict(FLEET)
        self.ready.add(role)
        log.info("%s fleet accepted", role)
        both_ready = len(self.ready) == len(ROLES)
        self._send_locked(conn, {
            "msg_type": "PLACEMENT_OK", "opponent_ready": both_ready,
            "message": ("Fleet accepted. Opponent is ready." if both_ready
                        else "Fleet accepted. Waiting for opponent.")})
        if both_ready:
            self.active_turn = random.choice(ROLES)
            self.fsm_state = State.PLAYER_TURN
            log.info("Battle starts: %s fires first (state=PLAYER_TURN)",
                     self.active_turn)
            self._broadcast_locked({
                "msg_type": "BATTLE_START", "active_turn": self.active_turn,
                "message": "All ships placed. %s fires first."
                           % self.active_turn.replace("_", " ")})

    def _apply_move_locked(self, conn, cell):
        shooter = conn.role
        defender = other_role(shooter)
        row, col = cell
        self.fsm_state = State.EVALUATE_MOVE
        self.shots[shooter].add(cell)
        ship = self.boards[defender][row][col]
        result, sunk = "MISS", None
        if ship is not None:
            self.round_hits[shooter] += 1
            self.ships_left[defender][ship] -= 1
            result = "HIT"
            if self.ships_left[defender][ship] == 0:
                result, sunk = "SUNK", ship
        remaining = self._ships_remaining_locked()
        won = remaining[defender] == 0
        log.info("%s fires (%d,%d): %s%s", shooter, row, col, result,
                 " (%s)" % sunk if sunk else "")
        # One STATE_UPDATE carries the shot AND the full score table, and goes
        # to both clients before the lock is released.
        self._broadcast_locked({
            "msg_type": "STATE_UPDATE", "shooter": shooter,
            "row": row, "col": col, "last_move_result": result,
            "sunk_ship": sunk,
            # null on the final shot: GAME_OVER follows immediately
            "active_turn": None if won else defender,
            "hits": dict(self.round_hits),
            "ships_remaining": remaining,
            "wins": self._wins_locked()})
        if won:
            self._finish_round_locked(shooter, "ALL_SHIPS_SUNK")
        else:
            self.active_turn = defender
            self.fsm_state = State.PLAYER_TURN

    def _finish_round_locked(self, winner, reason):
        """Record the win, broadcast GAME_OVER, then CLEANUP."""
        winner_conn = self.clients.get(winner)
        if winner_conn is not None:
            name = winner_conn.name
            self.session_wins[name] = self.session_wins.get(name, 0) + 1
        self.fsm_state = State.GAME_OVER
        log.info("GAME_OVER: %s wins (%s)", winner, reason)
        if winner_conn is not None:
            self._broadcast_locked({
                "msg_type": "GAME_OVER", "winner": winner, "reason": reason,
                "final_hits": dict(self.round_hits),
                "wins": self._wins_locked()})
        self._cleanup_locked()

    # -- shared helpers (lock must be held) -------------------------------
    def _reset_round_locked(self):
        """Clear all per-round state; ``session_wins`` is kept."""
        self.clients = {}                          # role -> Connection
        self.boards = {}                           # role -> 10x10 grid
        self.shots = {r: set() for r in ROLES}     # cells each player fired
        self.ships_left = {}                       # role -> {ship: cells}
        self.ready = set()
        self.active_turn = None
        self.round_hits = {r: 0 for r in ROLES}

    def _cleanup_locked(self):
        """CLEANUP: close sockets, reset round, return to the lobby."""
        self.fsm_state = State.CLEANUP
        for conn in list(self.clients.values()):
            self._close_conn(conn)
        self._reset_round_locked()
        self.fsm_state = State.WAITING_FOR_PLAYERS
        log.info("Round reset (state=WAITING_FOR_PLAYERS); session wins: %s",
                 self.session_wins)

    def _ships_remaining_locked(self):
        return {r: sum(1 for left in self.ships_left[r].values() if left > 0)
                for r in ROLES}

    def _wins_locked(self):
        return {r: self.session_wins.get(self.clients[r].name, 0)
                if r in self.clients else 0 for r in ROLES}

    def _broadcast_locked(self, msg):
        """Send the same message to both players, in a fixed order."""
        for role in ROLES:
            if role in self.clients:
                self._send_locked(self.clients[role], msg)

    def _send_locked(self, conn, msg):
        """Send one frame; a dead peer never hangs or crashes the server.

        On failure the socket is shut down, which wakes that client's handler
        thread with EOF, and that thread runs the normal forfeit path in
        ``handle_disconnect`` (no recursion inside the lock).
        """
        if conn.closed:
            return False
        try:
            send_message(conn.sock, msg)
            return True
        except OSError as exc:        # Reset, BrokenPipe, send timeout
            log.warning("Send to %s failed: %s", conn.label(), exc)
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return False

    def _close_conn(self, conn):
        """Shutdown (wakes a blocked recv in its thread) then close."""
        if conn.closed:
            return
        conn.closed = True
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def port_number(text):
    """argparse type: a TCP port in 1-65535."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


def parse_args(argv=None):
    """Parse ``-h`` (built in) and ``-p``."""
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Networked Battleship game server (2 players, TCP).")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help="listening port (binds to 0.0.0.0)")
    return parser.parse_args(argv)


def main(argv=None):
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    server = GameServer("0.0.0.0", args.port)
    try:
        server.start()
    except OSError as exc:
        print("[ERROR] Cannot listen on port %d: %s" % (args.port, exc),
              file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
