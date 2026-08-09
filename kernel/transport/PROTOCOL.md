# The wire

Every module is a process that reads requests on stdin and writes responses on
stdout, one JSON object per line. That is the entire interoperability contract,
and it is deliberately small enough to implement in an afternoon in any language
that can parse JSON and read a line.

```
→  {"id":1,"op":"summarise","in":{"before":"a\nb","after":"a\nc"}}
←  {"id":1,"out":{"added":1,"removed":1,"unchanged":1,"changed":true,"summary":"+1 −1"}}

→  {"id":2,"op":"summarise","in":{"before":null}}
←  {"id":2,"error":{"code":"EBADINPUT","message":"before must be a string"}}
```

One handshake operation is required of everybody:

```
→  {"id":0,"op":"__describe"}
←  {"id":0,"out":{"module":"summarise-diff","operations":["summarise"]}}
```

## Rules

- **One line per message.** No length prefixes, no framing headers. A message
  containing a newline must escape it, which JSON already does.
- **stdout is only for responses.** Anything a module wants to say to a human
  goes to stderr. A stray `print()` on stdout corrupts the stream, and the
  conformance driver treats that as a failure rather than trying to recover —
  a transport that guesses is a transport that eventually guesses wrong.
- **Responses may arrive in any order**, which is what `id` is for. The driver
  sends one at a time, but nothing in the protocol requires that.
- **An error is a response, not a crash.** A declared error from `interface.json`
  comes back as `{"error":{"code":…}}` with the process still alive. Exiting is
  reserved for the module being genuinely unable to continue.
- **Exit 0 on EOF.** When stdin closes, the module stops.

## Why not gRPC, HTTP, or a message bus

Because none of them are needed for what this buys, and each costs something
this design specifically does not want to pay.

The properties actually required are process isolation (a module that hangs or
crashes cannot take anything else with it) and language freedom (a module can be
written in whatever suits it). A pipe gives both. What a pipe does not give is
independent deployment — and that is the thing whose absence removes the need for
service discovery, version negotiation, compatibility windows, and a broker.

Discovery in particular is worth naming: it is the mechanism by which topology
stops being a file a human owns. There is no discovery here. A parent spawns a
child it named, and every edge is written down in `wiring.toml`.

## Adding a language

Two small files in this directory:

- `serve.<ext>` — imports the module and answers requests on stdin.
- `drive.<ext>` — reads the conformance suite, spawns `serve`, checks the answers.

Then add the name to `SUPPORTED_LANGUAGES` in `kernel/contract.js`. Both files are
kernel code: hand-written, trusted, and small enough to read in one sitting,
because a driver that is subtly more lenient than its sibling would admit a
module on weaker evidence than another language's. `kernel/kernel.test.js` runs
every driver against the same deliberately-wrong module and requires them all to
reject it.
