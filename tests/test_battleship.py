#!/usr/bin/env python3
"""Sprint 3 test suite: drives server.py over real localhost TCP sockets.

Run from the repository root:
    python3 -m unittest discover -s tests -v
"""

import json
import logging
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch
import client  # noqa: E402


from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402  (needs the sys.path tweak above)

logging.disable(logging.CRITICAL)

# Both players use this layout: ships stacked on rows 0-4, columns 0..size-1.
LAYOUT = [
    {"name": "CARRIER", "row": 0, "col": 0, "orientation": "H"},
    {"name": "BATTLESHIP", "row": 1, "col": 0, "orientation": "H"},
    {"name": "CRUISER", "row": 2, "col": 0, "orientation": "H"},
    {"name": "SUBMARINE", "row": 3, "col": 0, "orientation": "H"},
    {"name": "DESTROYER", "row": 4, "col": 0, "orientation": "H"},
]
SHIP_CELLS = [(r, c) for r, size in enumerate((5, 4, 3, 3, 2))
              for c in range(size)]
WATER_CELLS = [(r, c) for r in range(5, 10) for c in range(10)]
OTHER = {"Player_1": "Player_2", "Player_2": "Player_1"}


class RawClient:
    """Bare-bones protocol client so tests control every byte."""

    def __init__(self, port, timeout=3.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout)
        self.sock.settimeout(timeout)
        self.role = None

    def send(self, msg):
        data = json.dumps(msg).encode()
        self.sock.sendall(struct.pack("!I", len(data)) + data)

    def send_raw(self, data):
        self.sock.sendall(data)

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("closed")
            buf += chunk
        return buf

    def recv(self):
        (length,) = struct.unpack("!I", self._read(4))
        return json.loads(self._read(length).decode())

    def expect(self, msg_type):
        msg = self.recv()
        assert msg["msg_type"] == msg_type, "wanted %s got %r" % (
            msg_type, msg)
        return msg

    def expect_error(self, code):
        msg = self.expect("ERROR")
        assert msg["error_code"] == code, "wanted %s got %r" % (code, msg)
        return msg

    def expect_silence(self, seconds=0.4):
        self.sock.settimeout(seconds)
        try:
            self.recv()
        except socket.timeout:
            return
        finally:
            self.sock.settimeout(3.0)
        raise AssertionError("received an unexpected message")

    def expect_eof(self):
        try:
            self.recv()
        except EOFError:
            return
        except ConnectionError:
            return
        raise AssertionError("expected the connection to be closed")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def abort(self):
        """Close with TCP RST instead of FIN (simulates a network drop)."""
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack("ii", 1, 0))
        self.sock.close()

    # protocol conveniences
    def connect_as(self, name):
        self.send({"msg_type": "CONNECT", "player_id": name,
                   "timestamp": int(time.time())})

    def place(self, ships=None):
        self.send({"msg_type": "PLACE_SHIPS", "player_id": self.role,
                   "payload": {"ships": ships or LAYOUT}})

    def fire(self, row, col, player_id=None):
        self.send({"msg_type": "MOVE", "player_id": player_id or self.role,
                   "payload": {"row": row, "col": col}})


class ServerTestCase(unittest.TestCase):
    """Starts a fresh server on an ephemeral port for every test."""

    def setUp(self):
        self.server = server.GameServer("127.0.0.1", 0)
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.thread.join(timeout=3)

    def new_client(self):
        client = RawClient(self.server.port)
        self.clients.append(client)
        return client

    def join_two(self, name1="Alice", name2="Bob"):
        c1, c2 = self.new_client(), self.new_client()
        c1.connect_as(name1)
        c1.role = c1.expect("JOINED")["assigned_role"]
        c1.expect("LOBBY_WAIT")
        c2.connect_as(name2)
        c2.role = c2.expect("JOINED")["assigned_role"]
        c2.expect("GAME_START")
        c1.expect("GAME_START")
        return {c1.role: c1, c2.role: c2}

    def start_battle(self, name1="Alice", name2="Bob"):
        players = self.join_two(name1, name2)
        p1, p2 = players["Player_1"], players["Player_2"]
        p1.place()
        self.assertFalse(p1.expect("PLACEMENT_OK")["opponent_ready"])
        p2.place()
        self.assertTrue(p2.expect("PLACEMENT_OK")["opponent_ready"])
        start1, start2 = p1.expect("BATTLE_START"), p2.expect("BATTLE_START")
        self.assertEqual(start1["active_turn"], start2["active_turn"])
        self.assertIn(start1["active_turn"], server.ROLES)
        return players, start1["active_turn"]

    def wait_for_lobby(self):
        """Poll until the server has reset to WAITING_FOR_PLAYERS."""
        for _ in range(50):
            with self.server.state_lock:
                if (self.server.fsm_state ==
                        server.State.WAITING_FOR_PLAYERS
                        and not self.server.clients):
                    return
            time.sleep(0.05)
        self.fail("server never returned to WAITING_FOR_PLAYERS")


class FramingTests(ServerTestCase):
    def test_two_frames_in_one_tcp_segment(self):
        """Coalesced frames are split correctly by the length prefix."""
        c = self.new_client()
        frames = b""
        for msg in ({"msg_type": "CONNECT", "player_id": "Alice"},
                    {"msg_type": "MOVE", "player_id": "Player_1",
                     "payload": {"row": 0, "col": 0}}):
            data = json.dumps(msg).encode()
            frames += struct.pack("!I", len(data)) + data
        c.send_raw(frames)
        c.expect("JOINED")
        c.expect("LOBBY_WAIT")
        c.expect_error("WRONG_PHASE")

    def test_frame_fragmented_byte_by_byte(self):
        """A frame dribbled in one byte at a time is reassembled."""
        c = self.new_client()
        data = json.dumps({"msg_type": "CONNECT", "player_id": "Al"}).encode()
        for byte in struct.pack("!I", len(data)) + data:
            c.send_raw(bytes([byte]))
            time.sleep(0.002)
        self.assertEqual(c.expect("JOINED")["assigned_role"], "Player_1")

    def test_bad_json_returns_error_and_connection_survives(self):
        c = self.new_client()
        junk = b"this is not json"
        c.send_raw(struct.pack("!I", len(junk)) + junk)
        c.expect_error("MALFORMED_MESSAGE")
        c.connect_as("Alice")
        c.expect("JOINED")

    def test_unknown_msg_type_and_non_object(self):
        c = self.new_client()
        c.send({"msg_type": "TELEPORT"})
        c.expect_error("MALFORMED_MESSAGE")
        c.send([1, 2, 3])
        c.expect_error("MALFORMED_MESSAGE")

    def test_illegal_length_prefix_drops_connection(self):
        c = self.new_client()
        c.send_raw(struct.pack("!I", 0))
        c.expect_error("MALFORMED_MESSAGE")
        c.expect_eof()


class LobbyTests(ServerTestCase):
    def test_join_flow_and_roles(self):
        players = self.join_two("Alice", "Bob")
        self.assertEqual(set(players), {"Player_1", "Player_2"})

    def test_game_start_names_the_opponent(self):
        c1, c2 = self.new_client(), self.new_client()
        c1.connect_as("Alice")
        c1.expect("JOINED")
        c1.expect("LOBBY_WAIT")
        c2.connect_as("Bob")
        c2.expect("JOINED")
        self.assertEqual(c2.expect("GAME_START")["opponent_name"], "Alice")
        self.assertEqual(c1.expect("GAME_START")["opponent_name"], "Bob")

    def test_third_client_gets_server_full_and_game_is_unaffected(self):
        players = self.join_two()
        third = self.new_client()
        third.connect_as("Carol")
        third.expect_error("SERVER_FULL")
        third.expect_eof()
        players["Player_1"].place()
        players["Player_1"].expect("PLACEMENT_OK")

    def test_move_before_connect_is_wrong_phase(self):
        c = self.new_client()
        c.fire(0, 0, player_id="Player_1")
        c.expect_error("WRONG_PHASE")

    def test_bad_display_name_rejected(self):
        c = self.new_client()
        for bad in ("", "   ", 42, "x" * 40):
            c.send({"msg_type": "CONNECT", "player_id": bad})
            c.expect_error("MALFORMED_MESSAGE")

    def test_second_connect_on_same_socket_rejected(self):
        c = self.new_client()
        c.connect_as("Alice")
        c.expect("JOINED")
        c.expect("LOBBY_WAIT")
        c.connect_as("Alice")
        c.expect_error("WRONG_PHASE")

    def test_lone_client_disconnect_resets_lobby(self):
        c = self.new_client()
        c.connect_as("Alice")
        c.expect("JOINED")
        c.close()
        self.wait_for_lobby()
        d = self.new_client()
        d.connect_as("Dave")
        self.assertEqual(d.expect("JOINED")["assigned_role"], "Player_1")

    def test_place_ships_in_lobby_is_wrong_phase(self):
        c = self.new_client()
        c.connect_as("Alice")
        c.role = c.expect("JOINED")["assigned_role"]
        c.expect("LOBBY_WAIT")
        c.place()
        c.expect_error("WRONG_PHASE")


class PlacementTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.players = self.join_two()
        self.p1 = self.players["Player_1"]

    def with_ship(self, index, **changes):
        ships = [dict(s) for s in LAYOUT]
        ships[index].update(changes)
        return ships

    def test_invalid_fleets_are_rejected_and_state_unchanged(self):
        overlap = self.with_ship(1, row=0)
        off_board = self.with_ship(0, col=7)
        off_board_v = self.with_ship(4, row=9, orientation="V")
        wrong_set = LAYOUT[:4]
        duplicate = [dict(s) for s in LAYOUT]
        duplicate[4]["name"] = "CARRIER"
        unknown = self.with_ship(0, name="TUGBOAT")
        for bad in (overlap, off_board, off_board_v, wrong_set, duplicate,
                    unknown):
            self.p1.place(bad)
            self.p1.expect_error("INVALID_PLACEMENT")
        # still in PLACING_SHIPS: a valid fleet is now accepted
        self.p1.place()
        self.p1.expect("PLACEMENT_OK")

    def test_malformed_placement_payloads(self):
        base = {"msg_type": "PLACE_SHIPS", "player_id": "Player_1"}
        bad_messages = [
            dict(base),                                   # no payload
            dict(base, payload={}),                       # no ships
            dict(base, payload={"ships": "nope"}),
            dict(base, payload={"ships": [5]}),
            dict(base, payload={"ships": [{"name": "CARRIER"}]}),
            dict(base, payload={"ships": self.with_ship(0, row="0")}),
            dict(base, payload={"ships": self.with_ship(0, row=True)}),
            dict(base, payload={"ships": self.with_ship(
                0, orientation="D")}),
        ]
        for msg in bad_messages:
            self.p1.send(msg)
            self.p1.expect_error("MALFORMED_MESSAGE")

    def test_cannot_place_as_the_other_player(self):
        self.p1.send({"msg_type": "PLACE_SHIPS", "player_id": "Player_2",
                      "payload": {"ships": LAYOUT}})
        self.p1.expect_error("MALFORMED_MESSAGE")

    def test_second_submission_rejected(self):
        self.p1.place()
        self.p1.expect("PLACEMENT_OK")
        self.p1.place()
        self.p1.expect_error("INVALID_PLACEMENT")

    def test_move_during_placement_is_wrong_phase(self):
        self.p1.fire(0, 0)
        self.p1.expect_error("WRONG_PHASE")

    def test_vertical_ships_accepted(self):
        ships = [
            {"name": "CARRIER", "row": 0, "col": 0, "orientation": "V"},
            {"name": "BATTLESHIP", "row": 0, "col": 1, "orientation": "V"},
            {"name": "CRUISER", "row": 0, "col": 2, "orientation": "V"},
            {"name": "SUBMARINE", "row": 0, "col": 3, "orientation": "V"},
            {"name": "DESTROYER", "row": 0, "col": 8, "orientation": "V"},
        ]
        self.p1.place(ships)
        self.p1.expect("PLACEMENT_OK")


class BattleTests(ServerTestCase):
    def test_out_of_turn_move_only_errors_the_sender(self):
        players, active = self.start_battle()
        idle = OTHER[active]
        players[idle].fire(9, 9)
        players[idle].expect_error("NOT_YOUR_TURN")
        players[active].expect_silence()

    def test_invalid_moves_leave_the_turn_unchanged(self):
        players, active = self.start_battle()
        me = players[active]
        for row, col in ((10, 0), (0, 10), (-1, 3), (3, -1), (99, 99)):
            me.fire(row, col)
            me.expect_error("INVALID_MOVE")
        for payload in ({"row": "1", "col": 1}, {"row": 1.5, "col": 1},
                        {"row": 1}, {}, None):
            me.send({"msg_type": "MOVE", "player_id": active,
                     "payload": payload})
            me.expect_error("MALFORMED_MESSAGE")
        me.fire(9, 9)                       # still our turn, still legal
        self.assertEqual(me.expect("STATE_UPDATE")["last_move_result"],
                         "MISS")

    def test_repeat_cell_rejected_for_shooter_only(self):
        players, active = self.start_battle()
        idle = OTHER[active]
        players[active].fire(9, 9)
        for role in server.ROLES:
            players[role].expect("STATE_UPDATE")
        players[idle].fire(9, 9)            # different player: same cell OK
        for role in server.ROLES:
            players[role].expect("STATE_UPDATE")
        players[active].fire(9, 9)          # now a genuine repeat
        players[active].expect_error("INVALID_MOVE")

    def test_cannot_impersonate_the_opponent(self):
        players, active = self.start_battle()
        players[active].fire(9, 9, player_id=OTHER[active])
        players[active].expect_error("MALFORMED_MESSAGE")

    def test_state_update_contents_and_turn_switch(self):
        players, active = self.start_battle()
        idle = OTHER[active]
        players[active].fire(0, 0)          # opponent CARRIER is here: HIT
        u1 = players["Player_1"].expect("STATE_UPDATE")
        u2 = players["Player_2"].expect("STATE_UPDATE")
        self.assertEqual(u1, u2)            # identical broadcast
        self.assertEqual(u1["shooter"], active)
        self.assertEqual((u1["row"], u1["col"]), (0, 0))
        self.assertEqual(u1["last_move_result"], "HIT")
        self.assertIsNone(u1["sunk_ship"])
        self.assertEqual(u1["active_turn"], idle)
        self.assertEqual(u1["hits"][active], 1)
        self.assertEqual(u1["ships_remaining"], {"Player_1": 5,
                                                 "Player_2": 5})
        self.assertEqual(u1["wins"], {"Player_1": 0, "Player_2": 0})

    def test_full_game_scores_sinking_and_session_wins(self):
        players, shooter = self.start_battle("Alice", "Bob")
        target = OTHER[shooter]
        names = {shooter: "Alice" if players[shooter] is
                 self.clients[0] else "Bob"}
        sunk_at = {4: "CARRIER", 8: "BATTLESHIP", 11: "CRUISER",
                   14: "SUBMARINE", 16: "DESTROYER"}
        sunk_seen = []
        for i, (row, col) in enumerate(SHIP_CELLS):
            players[shooter].fire(row, col)
            u1 = players["Player_1"].expect("STATE_UPDATE")
            u2 = players["Player_2"].expect("STATE_UPDATE")
            self.assertEqual(u1, u2)
            self.assertEqual(u1["hits"][shooter], i + 1)
            expected = "SUNK" if i in sunk_at else "HIT"
            self.assertEqual(u1["last_move_result"], expected)
            if expected == "SUNK":
                sunk_seen.append(u1["sunk_ship"])
                self.assertEqual(u1["sunk_ship"], sunk_at[i])
            afloat = 5 - len(sunk_seen)
            self.assertEqual(u1["ships_remaining"][target], afloat)
            if i == len(SHIP_CELLS) - 1:
                self.assertIsNone(u1["active_turn"])
                break
            self.assertEqual(u1["active_turn"], target)
            players[target].fire(*WATER_CELLS[i])     # opponent only misses
            m1 = players["Player_1"].expect("STATE_UPDATE")
            m2 = players["Player_2"].expect("STATE_UPDATE")
            self.assertEqual(m1, m2)
            self.assertEqual(m1["last_move_result"], "MISS")
            self.assertEqual(m1["active_turn"], shooter)
        self.assertEqual(sunk_seen, list(sunk_at.values()))
        for role in server.ROLES:
            over = players[role].expect("GAME_OVER")
            self.assertEqual(over["winner"], shooter)
            self.assertEqual(over["reason"], "ALL_SHIPS_SUNK")
            self.assertEqual(over["final_hits"], {shooter: 17, target: 0})
            self.assertEqual(over["wins"], {shooter: 1, target: 0})
            players[role].expect_eof()                # CLEANUP closed it
        self.wait_for_lobby()

        # Session wins survive CLEANUP and follow the display name.
        winner_name = names[shooter]
        loser_name = "Bob" if winner_name == "Alice" else "Alice"
        again, active = self.start_battle(loser_name, winner_name)
        winner_role = next(r for r in server.ROLES
                           if again[r] is self.clients[-1])
        again[active].fire(9, 9)
        update = again["Player_1"].expect("STATE_UPDATE")
        self.assertEqual(update["wins"][winner_role], 1)
        self.assertEqual(update["wins"][OTHER[winner_role]], 0)

    def test_concurrent_moves_never_corrupt_state(self):
        """Both clients hammer MOVE at once; the lock keeps it consistent."""
        players, _ = self.start_battle()
        cells = {"Player_1": WATER_CELLS[:20], "Player_2": WATER_CELLS[20:40]}
        barrier = threading.Barrier(2)

        def spam(role):
            barrier.wait()
            for row, col in cells[role]:
                players[role].fire(row, col)

        threads = [threading.Thread(target=spam, args=(r,))
                   for r in server.ROLES]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        def drain(client):
            msgs = []
            client.sock.settimeout(0.6)
            try:
                while True:
                    msgs.append(client.recv())
            except socket.timeout:
                pass
            finally:
                client.sock.settimeout(3.0)
            return msgs

        got = {r: drain(players[r]) for r in server.ROLES}
        updates = {r: [m for m in got[r] if m["msg_type"] == "STATE_UPDATE"]
                   for r in server.ROLES}
        # Both screens saw the identical sequence of shots.
        self.assertEqual(updates["Player_1"], updates["Player_2"])
        shooters = [u["shooter"] for u in updates["Player_1"]]
        self.assertGreater(len(shooters), 0)
        for a, b in zip(shooters, shooters[1:]):
            self.assertNotEqual(a, b, "turns must strictly alternate")
        # Every request got exactly one reply: a broadcast or a rejection.
        for role in server.ROLES:
            mine = sum(1 for u in updates[role] if u["shooter"] == role)
            errors = [m for m in got[role] if m["msg_type"] == "ERROR"]
            self.assertTrue(all(e["error_code"] == "NOT_YOUR_TURN"
                                for e in errors))
            self.assertEqual(mine + len(errors), 20)


class ForfeitTests(ServerTestCase):
    def assert_forfeit(self, winner_client, winner_role):
        over = winner_client.expect("GAME_OVER")
        self.assertEqual(over["winner"], winner_role)
        self.assertEqual(over["reason"], "FORFEIT")
        self.assertEqual(over["wins"][winner_role], 1)
        winner_client.expect_eof()
        self.wait_for_lobby()

    def test_clean_close_during_battle(self):
        players, active = self.start_battle()
        players["Player_1"].close()
        self.assert_forfeit(players["Player_2"], "Player_2")

    def test_abrupt_reset_during_battle(self):
        players, active = self.start_battle()
        players["Player_2"].abort()
        self.assert_forfeit(players["Player_1"], "Player_1")

    def test_disconnect_message_during_battle(self):
        players, active = self.start_battle()
        loser = players[active]
        loser.send({"msg_type": "DISCONNECT", "player_id": active,
                    "reason": "QUIT"})
        self.assert_forfeit(players[OTHER[active]], OTHER[active])

    def test_disconnect_during_placement(self):
        players = self.join_two()
        players["Player_2"].close()
        self.assert_forfeit(players["Player_1"], "Player_1")

    def test_server_accepts_new_game_after_forfeit(self):
        players, _ = self.start_battle()
        players["Player_1"].close()
        players["Player_2"].expect("GAME_OVER")
        self.wait_for_lobby()
        self.join_two("Carol", "Dave")

    def test_both_players_vanish(self):
        players, _ = self.start_battle()
        players["Player_1"].abort()
        players["Player_2"].abort()
        self.wait_for_lobby()
        self.join_two("Erin", "Frank")


class CommandLineTests(unittest.TestCase):
    """Sprint 3 section 2.1 / 2.2 requirements, via real subprocesses."""

    def run_script(self, script, *args, stdin_text="", timeout=20):
        return subprocess.run(
            [sys.executable, str(ROOT / script), *args], input=stdin_text,
            capture_output=True, text=True, timeout=timeout, cwd=ROOT)

    def test_help_flags(self):
        for script in ("client.py", "server.py"):
            result = self.run_script(script, "-h")
            self.assertEqual(result.returncode, 0)
            self.assertIn("-p", result.stdout)
        self.assertIn("-i", self.run_script("client.py", "-h").stdout)

    def test_required_arguments(self):
        self.assertEqual(self.run_script("server.py").returncode, 2)
        self.assertEqual(self.run_script("client.py", "-p", "80").returncode,
                         2)
        self.assertEqual(
            self.run_script("client.py", "-i", "localhost").returncode, 2)
        self.assertEqual(
            self.run_script("server.py", "-p", "99999").returncode, 2)
        self.assertEqual(
            self.run_script("server.py", "-p", "abc").returncode, 2)

    def test_dns_failure_exits_one_without_traceback(self):
        for bad in ("no-such-host.invalid", "..bad", "a" * 80 + ".com"):
            result = self.run_script("client.py", "-i", bad, "-p", "8080")
            self.assertEqual(result.returncode, 1, bad)
            self.assertIn("[ERROR] Unable to resolve DNS hostname",
                          result.stdout)
            self.assertIn("Cisco router DNS configuration", result.stdout)
            self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_connection_refused_is_graceful(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result = self.run_script("client.py", "-i", "localhost", "-p",
                                 str(port), "-n", "Al")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not connect", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_server_reports_port_in_use(self):
        blocker = socket.socket()
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        try:
            port = blocker.getsockname()[1]
            result = self.run_script("server.py", "-p", str(port))
        finally:
            blocker.close()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot listen", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class EndToEndClientTests(unittest.TestCase):
    """Two real client.py processes play against the in-process server."""

    def test_two_clients_play_three_turns_then_one_quits(self):
        game = server.GameServer("127.0.0.1", 0)
        game.start()
        thread = threading.Thread(target=game.serve_forever, daemon=True)
        thread.start()
        try:
            moves = "5 0\n5 1\n5 2\n"
            procs = []
            for name in ("Alice", "Bob"):
                procs.append(subprocess.Popen(
                    [sys.executable, str(ROOT / "client.py"), "-i",
                     "localhost", "-p", str(game.port), "-n", name],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, cwd=ROOT))
                time.sleep(0.2)
            for proc in procs:               # feed BOTH before waiting on
                proc.stdin.write("auto\n" + moves)   # either, or the game
                proc.stdin.close()                    # cannot progress
            outputs = []
            for proc in procs:
                out = proc.stdout.read()
                err = proc.stderr.read()
                proc.wait(timeout=20)
                proc.stdout.close()
                proc.stderr.close()
                self.assertEqual(proc.returncode, 0, err)
                self.assertNotIn("Traceback", err)
                outputs.append(out)
            for out in outputs:
                self.assertIn("GAME START", out)
                self.assertIn("BATTLE START", out)
                self.assertIn("YOUR TURN", out)
                self.assertIn("Waiting for", out)
                self.assertGreaterEqual(out.count("fired at"), 3)
            # Alice quit first when her moves ran out; Bob wins by forfeit
            # or vice versa. Exactly one side reports a forfeit win.
            wins = sum("YOU WIN!" in out for out in outputs)
            self.assertEqual(wins, 1)
            self.assertEqual(
                sum("round ends by forfeit" in o for o in outputs), 1)
        finally:
            game.shutdown()


if __name__ == "__main__":
    unittest.main()

class ClientDNSTests(ServerTestCase):
    """Focused tests for client.py DNS resolution and graceful failure."""

    def test_resolve_hostname_uses_dns_for_reynolds_domain(self):
        """resolve_hostname delegates directly to socket.gethostbyname."""
        with patch("client.socket.gethostbyname",
                   return_value="10.0.0.50") as mock_dns:
            ip = client.resolve_hostname("server.reynolds.edu")
        self.assertEqual(ip, "10.0.0.50")
        mock_dns.assert_called_once_with("server.reynolds.edu")

    def test_resolve_hostname_catches_gaierror_gracefully(self):
        """Simulated DNS outage prints a friendly error and exits 1."""
        buf = io.StringIO()
        with patch("client.socket.gethostbyname",
                   side_effect=socket.gaierror(socket.EAI_NONAME,
                                               "Name or service not known")):
            with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                client.resolve_hostname("server.reynolds.edu")

        self.assertEqual(cm.exception.code, 1)
        out = buf.getvalue()
        self.assertIn("[ERROR] Unable to resolve DNS hostname "
                      "'server.reynolds.edu'.", out)
        self.assertIn("Cisco router DNS configuration", out)
        self.assertNotIn("Traceback", out)

    def test_resolve_hostname_catches_unicode_error_gracefully(self):
        """Malformed IDNA labels exit 1 cleanly instead of raising."""
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            client.resolve_hostname("..bad")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] Unable to resolve DNS hostname '..bad'.",
                      buf.getvalue())

    def test_blank_hostname_rejected_by_cli(self):
        """Empty/whitespace -i values are rejected before DNS lookup."""
        for bad in ("", "   "):
            result = subprocess.run(
                [sys.executable, str(ROOT / "client.py"),
                 "-i", bad, "-p", "8080"],
                capture_output=True, text=True, timeout=10, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 2, repr(bad))
            self.assertIn("hostname must not be empty", result.stderr)

    def test_main_resolves_hostname_and_connects_to_resolved_ip(self):
        """client.main resolves server.reynolds.edu and talks to the server."""
        # Occupy both slots so the client connects, receives SERVER_FULL,
        # and exits cleanly without requiring interactive gameplay input.
        self.join_two("Alice", "Bob")
        buf = io.StringIO()
        with patch("client.socket.gethostbyname",
                   return_value="127.0.0.1") as mock_dns:
            with redirect_stdout(buf):
                rc = client.main([
                    "-i", "server.reynolds.edu",
                    "-p", str(self.server.port),
                    "-n", "Austin",
                ])

        mock_dns.assert_called_once_with("server.reynolds.edu")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Resolved server.reynolds.edu -> 127.0.0.1", out)
        self.assertIn("SERVER_FULL", out)

    def test_server_disconnect_returns_exit_code_one(self):
        """If the server drops mid-session, client.main fails gracefully."""
        dummy_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dummy_server.bind(("127.0.0.1", 0))
        dummy_server.listen(1)
        port = dummy_server.getsockname()[1]

        def accept_and_drop():
            conn, _ = dummy_server.accept()
            conn.close()

        t = threading.Thread(target=accept_and_drop, daemon=True)
        t.start()
        buf = io.StringIO()
        try:
            with patch("client.socket.gethostbyname", return_value="127.0.0.1"):
                with redirect_stdout(buf):
                    rc = client.main([
                        "-i", "server.reynolds.edu",
                        "-p", str(port),
                        "-n", "Austin",
                    ])
        finally:
            t.join(timeout=2)
            dummy_server.close()

        self.assertEqual(rc, 1)
        self.assertIn("Lost connection to the server", buf.getvalue())