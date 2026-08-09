# Kernel shim: turn a Python module into a process that speaks the wire.
#
# The exact counterpart of serve.mjs, and it must behave identically — a shim
# that is subtly more forgiving than its sibling would let a module in one
# language be admitted on weaker evidence than the same module in another.
# See PROTOCOL.md.
#
# Trusted kernel code. Never delegated.

import importlib.util
import inspect
import json
import os
import sys


def _load(entry):
    spec = importlib.util.spec_from_file_location("module_under_test", os.path.abspath(entry))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {entry}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(obj):
    # stdout carries responses and nothing else. Anything a module wants to say
    # to a human belongs on stderr; a stray print() here corrupts the stream.
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    entry = sys.argv[1] if len(sys.argv) > 1 else "run.py"
    name = sys.argv[2] if len(sys.argv) > 2 else "module"

    try:
        impl = _load(entry)
    except Exception as err:                      # noqa: BLE001 — a dead module is a dead module
        # A module that will not even load is not a protocol error. This is one
        # of the few places exiting is right.
        sys.stderr.write(f"serve: cannot load {entry}: {err!r}\n")
        sys.exit(70)

    operations = sorted(
        n for n, v in vars(impl).items()
        if not n.startswith("_") and (inspect.isfunction(v) or inspect.isbuiltin(v))
        and getattr(v, "__module__", None) == impl.__name__
    )

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue

        try:
            req = json.loads(text)
        except json.JSONDecodeError:
            _write({"id": None, "error": {"code": "EPROTOCOL", "message": "request was not valid JSON"}})
            continue

        rid = req.get("id")

        if req.get("op") == "__describe":
            _write({"id": rid, "out": {"module": name, "operations": operations}})
            continue

        op = req.get("op")
        fn = getattr(impl, op, None) if isinstance(op, str) else None
        if not callable(fn) or op not in operations:
            exposed = ", ".join(operations) or "nothing"
            _write({"id": rid, "error": {"code": "ENOOP", "message": f"no operation {json.dumps(op)}; this module exposes {exposed}"}})
            continue

        try:
            out = fn(req.get("in"))
            _write({"id": rid, "out": out})
        except Exception as err:                  # noqa: BLE001
            # A declared error is a RESPONSE, not a crash. The process stays
            # alive, because "this input was rejected" and "this module is
            # broken" are different facts a caller must be able to tell apart.
            code = getattr(err, "code", None) or "EUNCAUGHT"
            _write({"id": rid, "error": {"code": code, "message": str(err)}})


if __name__ == "__main__":
    main()
