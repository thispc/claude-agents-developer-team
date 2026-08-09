# Kernel shim: run the language-neutral conformance suite against a module by
# talking to it over the wire.
#
#   python3 drive.py <conformance.json> <interface.json> <name> -- <serve command…>
#
# The exact twin of drive.mjs. It must behave IDENTICALLY: a driver that is
# subtly more forgiving than its sibling would admit a module in one language on
# weaker evidence than the same module in another, and nothing downstream would
# notice, because both would simply report success. kernel.test.js runs both
# against the same wrong module and requires both to reject it.
#
# Trusted kernel code. Never delegated.

import json
import subprocess
import sys
import threading


CASE_TIMEOUT_DEFAULT_MS = 10000


def mismatch(want, got, path=""):
    """`expect` is a SUBSET match: every key it names must be present and equal.

    Anything the case does not mention, it does not constrain — so a module may
    return extra fields, but never a wrong one.

    These rules must match drive.mjs exactly.
    """
    where = path or "the result"

    if want is None or not isinstance(want, (dict, list)):
        # bool is a subclass of int in Python and is not in JavaScript, so
        # compare types explicitly or True would equal 1 here and not there.
        if type(want) is not type(got) and not (isinstance(want, (int, float)) and isinstance(got, (int, float)) and not isinstance(want, bool) and not isinstance(got, bool)):
            return f"{where}: expected {json.dumps(want)}, got {json.dumps(got)}"
        return None if want == got else f"{where}: expected {json.dumps(want)}, got {json.dumps(got)}"

    if isinstance(want, list):
        if not isinstance(got, list):
            return f"{where}: expected an array, got {json.dumps(got)}"
        if len(want) != len(got):
            return f"{where}: expected {len(want)} item(s), got {len(got)}"
        for i, item in enumerate(want):
            bad = mismatch(item, got[i], f"{where}[{i}]")
            if bad:
                return bad
        return None

    if not isinstance(got, dict):
        return f"{where}: expected an object, got {json.dumps(got)}"
    for k in want:
        if k not in got:
            return f'{where}: missing "{k}"'
        bad = mismatch(want[k], got[k], f"{path}.{k}" if path else k)
        if bad:
            return bad
    return None


def contains(case, out):
    """`expectContains` / `expectNotContains` — substring assertions on named
    string fields, for modules whose output is text.

    Exact matching is right for structured output and useless for a rendered
    document: pinning a whole HTML page byte-for-byte makes every cosmetic edit a
    contract change, and pinning nothing makes the contract vacuous. What is
    worth stating about a page is that particular things appear in it and
    particular things never do — which is also the only way to assert escaping,
    since the interesting claim is the ABSENCE of a live tag.

    Must match drive.mjs exactly.
    """
    out = out or {}
    for field, needles in (case.get("expectContains") or {}).items():
        value = out.get(field)
        if not isinstance(value, str):
            return f'expectContains names "{field}", which is {"missing" if value is None else "not a string"}'
        for needle in needles:
            if needle not in value:
                return f'"{field}" does not contain {json.dumps(needle)}'
    for field, needles in (case.get("expectNotContains") or {}).items():
        value = out.get(field)
        if not isinstance(value, str):
            return f'expectNotContains names "{field}", which is {"missing" if value is None else "not a string"}'
        for needle in needles:
            if needle in value:
                return f'"{field}" contains {json.dumps(needle)}, which it must not'
    return None


def main():
    argv = sys.argv[1:]
    if "--" in argv:
        head, serve_cmd = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    else:
        head, serve_cmd = argv[:3], argv[3:]

    if len(head) < 2 or not serve_cmd:
        sys.stderr.write("drive: usage: drive.py <conformance.json> <interface.json> <name> -- <serve command…>\n")
        sys.exit(64)

    with open(head[0], encoding="utf-8") as fh:
        suite = json.load(fh)
    with open(head[1], encoding="utf-8") as fh:
        iface = json.load(fh)

    cases = suite.get("cases") or []
    timeout_s = float(suite.get("timeoutMs", CASE_TIMEOUT_DEFAULT_MS)) / 1000.0

    child = subprocess.Popen(serve_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    junk = []
    pending = []
    lock = threading.Condition()

    def reader():
        for line in child.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                # stdout is the wire. A line that is not JSON means the module
                # printed something there, and a transport that shrugs that off
                # is one that eventually mis-parses a real response.
                with lock:
                    junk.append(text)
                continue
            with lock:
                pending.append(msg)
                lock.notify_all()

    threading.Thread(target=reader, daemon=True).start()

    def send(req):
        child.stdin.write(json.dumps(req) + "\n")
        child.stdin.flush()
        with lock:
            if not lock.wait_for(lambda: len(pending) > 0, timeout=timeout_s):
                raise TimeoutError(f"no response within {int(timeout_s * 1000)}ms — the module hung")
            return pending.pop(0)

    failures = []
    passed = 0

    # Every operation the interface declares must actually be exposed. A module
    # that answers some of its interface is not a smaller module, it is a broken
    # one.
    declared = sorted((iface.get("operations") or {}).keys())
    try:
        described = send({"id": 0, "op": "__describe"})
    except Exception as err:  # noqa: BLE001
        described = {"error": {"code": "ETIMEOUT", "message": str(err)}}

    if described.get("error"):
        failures.append(f"__describe failed: {described['error'].get('code')} {described['error'].get('message')}")
    else:
        exposed = sorted((described.get("out") or {}).get("operations", []))
        missing = [o for o in declared if o not in exposed]
        # Extra operations are a failure too, not a bonus. The interface is the
        # front door and the ONLY way anything reaches this module; a helper that
        # leaks out of it is a second door nobody declared, and the next module
        # along will start depending on it.
        extra = [o for o in exposed if o not in declared]
        if missing:
            failures.append(f"interface declares {', '.join(missing)}, which the module does not expose")
        elif extra:
            failures.append(f"the module exposes {', '.join(extra)}, which the interface does not declare — the interface is the whole front door, so an undeclared operation is a second one")
        else:
            passed += 1

    for i, case in enumerate(cases):
        label = case.get("name") or f"case {i + 1}"
        try:
            res = send({"id": i + 1, "op": case.get("op"), "in": case.get("in")})
        except Exception as err:  # noqa: BLE001
            failures.append(f"{label}: {err}")
            continue

        if "expectError" in case:
            if not res.get("error"):
                failures.append(f"{label}: expected error {case['expectError']}, got a result: {json.dumps(res.get('out'))}")
            elif res["error"].get("code") != case["expectError"]:
                failures.append(f"{label}: expected error {case['expectError']}, got {res['error'].get('code')} ({res['error'].get('message')})")
            else:
                passed += 1
            continue

        if res.get("error"):
            failures.append(f"{label}: expected a result, got error {res['error'].get('code')}: {res['error'].get('message')}")
            continue

        # A case may pin structure, or text, or both. An absent `expect`
        # constrains nothing — it must not be read as "expected None".
        structural = mismatch(case["expect"], res.get("out")) if "expect" in case else None
        bad = structural or contains(case, res.get("out"))
        if bad:
            failures.append(f"{label}: {bad}")
        else:
            passed += 1

    with lock:
        if junk:
            failures.append(f"the module wrote {len(junk)} non-JSON line(s) to stdout, which corrupts the wire. First: {json.dumps(junk[0][:120])}")

    try:
        child.stdin.close()
        child.kill()
    except Exception:  # noqa: BLE001
        pass

    for f in failures:
        print(f"not ok - {f}")
    print(f"# {passed} passed, {len(failures)} failed")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
