#!/usr/bin/env python3
"""Networked Battleship client (CS 457, Sprint 3).

Speaks the protocol in ``docs/protocol_blueprint.md``: TCP, UTF-8 JSON,
4-byte big-endian length-prefixed frames (``!I``). The client is "dumb" on
purpose: it redraws only from messages the server sends, so both players'
screens always agree (docs/concurrency_design.md section 4.2).

Usage:
    python3 client.py -h
    python3 client.py -i server.reynolds.edu -p 8080
    python3 client.py -i localhost -p 8080 -n Austin
"""

import argparse
import json
import random
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Protocol / game constants (must match server.py)
# ---------------------------------------------------------------------------
HEADER = struct.Struct("!I")
MAX_PAYLOAD = 64 * 1024
CONNECT_TIMEOUT = 10.0

BOARD_SIZE = 10
FLEET = [("CARRIER", 5), ("BATTLESHIP", 4), ("CRUISER", 3),
         ("SUBMARINE", 3), ("DESTROYER", 2)]
WATER, SHIP, HIT, MISS = "~", "#", "X", "o"
QUIT_WORDS = {"quit", "exit", "q"}


class QuitGame(Exception):
    """The player asked to leave (typed quit, Ctrl+D or Ctrl+C)."""


# ---------------------------------------------------------------------------
# DNS resolution (Sprint 3 requirement 2.2: fail gracefully, exit code 1)
# ---------------------------------------------------------------------------
def resolve_hostname(hostname):
    """Resolve ``hostname`` with gethostbyname(); exit(1) cleanly on failure.

    ``UnicodeError`` is caught as well: a malformed name such as an empty or
    over-long label is rejected by the IDNA encoder before any DNS query is
    sent, and would otherwise print a raw traceback.
    """
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, UnicodeError):
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration "
              "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------
def recv_exact(sock, n):
    """Read exactly ``n`` bytes; raise EOFError if the server closes."""
    chunks = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("server closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock):
    """Read one length-prefixed JSON frame."""
    (length,) = HEADER.unpack(recv_exact(sock, HEADER.size))
    if length == 0 or length > MAX_PAYLOAD:
        raise ValueError(f"illegal frame length {length}")
    return json.loads(recv_exact(sock, length).decode("utf-8"))


def send_message(sock, msg):
    """Write one length-prefixed JSON frame."""
    data = json.dumps(msg).encode("utf-8")
    sock.sendall(HEADER.pack(len(data)) + data)


# ---------------------------------------------------------------------------
# Board helpers (display and local pre-validation only; the server is the
# authority and re-validates everything)
# ---------------------------------------------------------------------------
def ship_cells(row, col, size, orientation):
    """Cells covered by a ship (H grows along col, V grows along row)."""
    return [(row + (i if orientation == "V" else 0),
             col + (i if orientation == "H" else 0)) for i in range(size)]


def cells_error(cells, occupied):
    """Return a problem description for a proposed ship, or None."""
    for r, c in cells:
        if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
            return "That ship would hang off the board."
        if (r, c) in occupied:
            return "That ship would overlap another ship."
    return None


def random_fleet():
    """Place every ship at a random legal position."""
    ships, occupied = [], set()
    for name, size in FLEET:
        while True:
            orient = random.choice("HV")
            row = random.randrange(BOARD_SIZE)
            col = random.randrange(BOARD_SIZE)
            cells = ship_cells(row, col, size, orient)
            if cells_error(cells, occupied) is None:
                break
        ships.append({"name": name, "row": row, "col": col,
                      "orientation": orient})
        occupied.update(cells)
    return ships


def grid_from_ships(ships):
    """Build a display grid showing ship cells."""
    grid = [[WATER] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    sizes = dict(FLEET)
    for ship in ships:
        for r, c in ship_cells(ship["row"], ship["col"],
                               sizes[ship["name"]], ship["orientation"]):
            grid[r][c] = SHIP
    return grid


def render_boards(own, tracking, opponent):
    """Return two boards side by side as text."""
    cols = " ".join(str(c) for c in range(BOARD_SIZE))
    left_title = "YOUR FLEET".center(2 + 2 * BOARD_SIZE)
    right_title = f"{opponent}'s WATERS"[:2 + 2 * BOARD_SIZE].center(
        2 + 2 * BOARD_SIZE)
    lines = [f"    {left_title}      {right_title}",
             f"    {cols}        {cols}"]
    for r in range(BOARD_SIZE):
        left = " ".join(own[r])
        right = " ".join(tracking[r])
        lines.append(f"  {r} {left}      {r} {right}")
    lines.append(f"    ({SHIP} ship  {HIT} hit  {MISS} miss  {WATER} water)")
    return "\n".join(lines)


def ask(prompt):
    """input() wrapper: EOF or 'quit' raises QuitGame."""
    try:
        text = input(prompt).strip()
    except EOFError:
        raise QuitGame from None
    if not sys.stdin.isatty():
        print(text)                   # echo scripted input so logs read well
    if text.lower() in QUIT_WORDS:
        raise QuitGame
    return text


# ---------------------------------------------------------------------------
# Game client
# ---------------------------------------------------------------------------
class BattleshipClient:
    """Drives one player's session: one handler per server message type."""

    def __init__(self, sock, name):
        self.sock = sock
        self.name = name
        self.role = None
        self.opponent = "Opponent"
        self.my_ships = []
        self.pending_ships = []
        self.own_grid = [[WATER] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        self.track_grid = [[WATER] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        self.fired = set()
        self.awaiting = None          # "placement" or "move" (for ERROR retry)
        self.finished = False
        self.handlers = {
            "JOINED": self.on_joined,
            "LOBBY_WAIT": self.on_lobby_wait,
            "GAME_START": self.on_game_start,
            "PLACEMENT_OK": self.on_placement_ok,
            "BATTLE_START": self.on_battle_start,
            "STATE_UPDATE": self.on_state_update,
            "GAME_OVER": self.on_game_over,
            "ERROR": self.on_error,
        }

    # -- main loop ---------------------------------------------------------
    def run(self):
        """Send CONNECT, then react to server messages until GAME_OVER."""
        self.send({"msg_type": "CONNECT", "player_id": self.name,
                   "timestamp": int(time.time())})
        while not self.finished:
            msg = recv_message(self.sock)
            handler = self.handlers.get(msg.get("msg_type"))
            if handler:
                handler(msg)

    def send(self, msg):
        """Send a frame to the server."""
        send_message(self.sock, msg)

    def send_quit(self):
        """Tell the server we are leaving (counts as a forfeit)."""
        try:
            self.send({"msg_type": "DISCONNECT",
                       "player_id": self.role or self.name,
                       "reason": "QUIT"})
        except OSError:
            pass

    # -- message handlers --------------------------------------------------
    def on_joined(self, msg):
        self.role = msg["assigned_role"]
        print(f"[SERVER] {msg.get('message', '')} (you are {self.role})")

    def on_lobby_wait(self, msg):
        print(f"[SERVER] {msg.get('message', 'Waiting for Player 2...')}")

    def on_game_start(self, msg):
        self.opponent = msg.get("opponent_name", "Opponent")
        print(f"\n=== GAME START: you vs {self.opponent} ===")
        self.submit_fleet()

    def on_placement_ok(self, msg):
        self.awaiting = None
        self.my_ships = self.pending_ships
        self.own_grid = grid_from_ships(self.my_ships)
        if msg.get("opponent_ready"):
            print("Fleet accepted. Opponent is ready.")
        else:
            print(f"Fleet accepted. Waiting for {self.opponent} "
                  "to place their ships...")

    def on_battle_start(self, msg):
        print("\n=== BATTLE START ===")
        print(msg.get("message", ""))
        print(render_boards(self.own_grid, self.track_grid, self.opponent))
        self.announce_turn(msg.get("active_turn"))

    def on_state_update(self, msg):
        self.awaiting = None
        row, col = msg["row"], msg["col"]
        result = msg["last_move_result"]
        landed = result in ("HIT", "SUNK")
        mine = msg["shooter"] == self.role
        if mine:
            self.track_grid[row][col] = HIT if landed else MISS
            self.fired.add((row, col))
            print(f"\nYou fired at ({row},{col}): {result}!")
            if result == "SUNK":
                print(f"  You sunk their {msg['sunk_ship']}!")
        else:
            self.own_grid[row][col] = HIT if landed else MISS
            print(f"\n{self.opponent} fired at ({row},{col}): {result}!")
            if result == "SUNK":
                print(f"  They sunk your {msg['sunk_ship']}!")
        self.print_scores(msg)
        print(render_boards(self.own_grid, self.track_grid, self.opponent))
        self.announce_turn(msg.get("active_turn"))

    def on_error(self, msg):
        print(f"[SERVER ERROR] {msg.get('error_code')}: {msg.get('message')}")
        retry, self.awaiting = self.awaiting, None
        if msg.get("error_code") == "SERVER_FULL":
            self.finished = True
        elif retry == "placement":
            self.submit_fleet()
        elif retry == "move":
            self.take_turn()

    def on_game_over(self, msg):
        self.finished = True
        winner = msg["winner"]
        print("\n=== GAME OVER ===")
        if msg.get("reason") == "FORFEIT":
            print("Your opponent disconnected, so the round ends by forfeit.")
        outcome = "YOU WIN!" if winner == self.role else "You lose."
        print(outcome)
        opp_role = "Player_2" if self.role == "Player_1" else "Player_1"
        hits, wins = msg.get("final_hits", {}), msg.get("wins", {})
        print(f"Final hits : you {hits.get(self.role, 0)}"
              f" | {self.opponent} {hits.get(opp_role, 0)}")
        print(f"Session wins: you {wins.get(self.role, 0)}"
              f" | {self.opponent} {wins.get(opp_role, 0)}")

    # -- interaction -------------------------------------------------------
    def print_scores(self, msg):
        """One-line score summary taken from a STATE_UPDATE."""
        opp_role = "Player_2" if self.role == "Player_1" else "Player_1"
        hits, left, wins = (msg["hits"], msg["ships_remaining"], msg["wins"])
        print(f"  Hits: you {hits[self.role]} | {self.opponent} "
              f"{hits[opp_role]}   Ships afloat: you {left[self.role]} | "
              f"{self.opponent} {left[opp_role]}   Session wins: "
              f"you {wins[self.role]} | {self.opponent} {wins[opp_role]}")

    def announce_turn(self, active):
        """Show whose turn it is; prompt for a move if it is ours."""
        if active is None:
            return                    # final shot; GAME_OVER comes next
        if active == self.role:
            print("\n*** YOUR TURN ***")
            self.take_turn()
        else:
            print(f"\n... Waiting for {self.opponent} ({active}) "
                  "to fire ...")

    def submit_fleet(self):
        """Choose a fleet and send PLACE_SHIPS."""
        self.pending_ships = self.choose_fleet()
        self.awaiting = "placement"
        self.send({"msg_type": "PLACE_SHIPS", "player_id": self.role,
                   "payload": {"ships": self.pending_ships},
                   "timestamp": int(time.time())})

    def choose_fleet(self):
        """Interactively place all five ships ('auto' = random fleet)."""
        print("Place your fleet. For each ship enter:  row col H|V")
        print("  H extends right (increasing col), V extends down "
              "(increasing row). Example: 0 0 H")
        print("  Type 'auto' to place the whole fleet randomly.")
        ships, occupied = [], set()
        for name, size in FLEET:
            while True:
                print(render_boards(grid_from_ships(ships),
                                    [[WATER] * BOARD_SIZE] * BOARD_SIZE,
                                    self.opponent))
                raw = ask(f"Place {name} (length {size}): ")
                if raw.lower() == "auto":
                    return random_fleet()
                parts = raw.replace(",", " ").split()
                if (len(parts) != 3 or not parts[0].isdigit()
                        or not parts[1].isdigit()
                        or parts[2].upper() not in ("H", "V")):
                    print("Format is: row col H|V   (example: 3 4 V)")
                    continue
                row, col = int(parts[0]), int(parts[1])
                orient = parts[2].upper()
                cells = ship_cells(row, col, size, orient)
                problem = cells_error(cells, occupied)
                if problem:
                    print(problem)
                    continue
                ships.append({"name": name, "row": row, "col": col,
                              "orientation": orient})
                occupied.update(cells)
                break
        return ships

    def take_turn(self):
        """Prompt for coordinates and send a MOVE."""
        while True:
            raw = ask("Fire at (row col, 0-9): ")
            parts = raw.replace(",", " ").split()
            if (len(parts) != 2 or not all(p.isdigit() for p in parts)
                    or not all(int(p) < BOARD_SIZE for p in parts)):
                print("Enter two numbers from 0 to 9, e.g.  4 7")
                continue
            row, col = int(parts[0]), int(parts[1])
            if (row, col) in self.fired:
                print("You already fired there. Pick another cell.")
                continue
            break
        self.awaiting = "move"
        self.send({"msg_type": "MOVE", "player_id": self.role,
                   "payload": {"row": row, "col": col},
                   "timestamp": int(time.time())})


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


def hostname_arg(text):
    """argparse type: reject '' (gethostbyname('') gives 0.0.0.0)."""
    if not text.strip():
        raise argparse.ArgumentTypeError("hostname must not be empty")
    return text.strip()


def parse_args(argv=None):
    """Parse ``-h`` (built in), ``-i`` and ``-p`` (plus optional ``-n``)."""
    parser = argparse.ArgumentParser(
        prog="client.py",
        description="Networked Battleship client.")
    parser.add_argument("-i", "--hostname", dest="hostname", required=True,
                        type=hostname_arg,
                        help="server DNS hostname, e.g. server.reynolds.edu")
    parser.add_argument("-p", "--port", type=port_number, required=True,
                        help="server port")
    parser.add_argument("-n", "--name", default=None,
                        help="display name (prompted for if omitted)")
    return parser.parse_args(argv)


def connect(server_ip, port, hostname):
    """Open the TCP connection or exit(1) with a readable message."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect((server_ip, port))
    except OSError as exc:
        sock.close()
        print(f"[ERROR] Could not connect to {hostname} "
              f"({server_ip}) port {port}: {exc}")
        print("Check that server.py is running and that the port is "
              "reachable (router ACLs / firewall).")
        sys.exit(1)
    sock.settimeout(None)             # games can sit idle between turns
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def main(argv=None):
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    server_ip = resolve_hostname(args.hostname)
    print(f"Resolved {args.hostname} -> {server_ip}")
    sock = connect(server_ip, args.port, args.hostname)
    client = None
    try:
        name = args.name or ask("Enter your display name: ") or "Player"
        client = BattleshipClient(sock, name[:32])
        client.run()
        return 0
    except (QuitGame, KeyboardInterrupt):
        print("\nLeaving the game (this counts as a forfeit).")
        if client:
            client.send_quit()
        return 0
    except (EOFError, ConnectionError, socket.timeout) as exc:
        print(f"\n[ERROR] Lost connection to the server: {exc}")
        return 1
    except ValueError as exc:         # corrupt frame / bad JSON from server
        print(f"\n[ERROR] Received invalid data from the server: {exc}")
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
