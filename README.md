# CS 457 Networked Battleship (Sprint 3)

Two-player Battleship over TCP. Design documents are in `docs/`; the code
follows them: length-prefixed JSON framing, the FSM in
`docs/fsm_specification.md`, and a thread-per-client server guarded by one
lock (`docs/concurrency_design.md`).

## Run (localhost)

```bash
# terminal 1
python3 server.py -p 8080

# terminals 2 and 3
python3 client.py -i localhost -p 8080
```

Options: `server.py -h`, `client.py -h`. The client also accepts an optional
`-n NAME` to skip the name prompt. In Sprint 4 use the DNS name instead, e.g.
`python3 client.py -i server.reynolds.edu -p 8080`.

## Playing

* Place ships as `row col H|V` (H grows right, V grows down), or type `auto`.
* Fire with `row col` (0-9). Type `quit` at any prompt to leave (forfeit).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

`docs/sample_gameplay_log.txt` is a generated example of two clients playing
several turns.
