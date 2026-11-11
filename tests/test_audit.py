import json

from opspilot.audit import AuditLog


def test_tampering_is_detected(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.record("action_executed", n=i)
    assert log.verify()[0]

    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["n"] = 99
    lines[1] = json.dumps(entry, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    ok, msg = AuditLog(path).verify()
    assert not ok and "line 2" in msg


def test_deleted_line_breaks_chain(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.record("e", n=i)
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not AuditLog(path).verify()[0]


def test_chain_continues_across_restarts(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path).record("first")
    AuditLog(path).record("second")
    assert AuditLog(path).verify() == (True, "chain intact")
