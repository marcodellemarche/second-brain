import json
import threading
from pathlib import Path

import pytest

from second_brain.guard.audit import GENESIS, MAX_VALUE_CHARS, AuditLog, verify


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_first_line_points_at_genesis(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append({"event": "loaded"})
    first = read_lines(path)[0]
    assert first["prev"] == GENESIS
    assert first["event"] == "loaded"
    assert first["ts"].endswith("+00:00")


def test_append_returns_the_line_hash(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    digest = AuditLog(path).append({"event": "loaded"})
    assert digest == read_lines(path)[0]["hash"]
    assert verify(path) == (True, 1)


def test_chain_verifies(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(5):
        log.append({"event": "allow", "step": i})
    assert verify(path) == (True, 5)


def test_empty_or_missing_log_verifies(tmp_path) -> None:
    assert verify(tmp_path / "missing.jsonl") == (True, 0)
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert verify(path) == (True, 0)


def test_tampering_with_a_middle_line_is_detected_at_that_line(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(4):
        log.append({"event": "allow", "step": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["step"] = 99
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify(path) == (False, 2)


def test_removing_a_middle_line_is_detected_at_that_line(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(4):
        log.append({"event": "allow", "step": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    assert verify(path) == (False, 2)


def test_unparsable_line_is_detected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append({"event": "allow"})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    assert verify(path) == (False, 2)


def test_chain_continues_after_reopening(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append({"event": "allow", "step": 1})
    reopened = AuditLog(path)
    reopened.append({"event": "allow", "step": 2})
    assert verify(path) == (True, 2)
    lines = read_lines(path)
    assert lines[1]["prev"] == lines[0]["hash"]


def test_reopening_a_corrupt_tail_fails_closed(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text('{"event": "allow"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        AuditLog(path)


def test_concurrent_appends_keep_the_chain_intact(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    def work(worker: int) -> None:
        for step in range(25):
            log.append({"event": "allow", "worker": worker, "step": step})

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert verify(path) == (True, 100)


def test_long_values_are_truncated(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append({"event": "tool_result", "result": "x" * 5000, "nested": {"blob": "y" * 5000}})
    row = read_lines(path)[0]
    assert len(row["result"]) == MAX_VALUE_CHARS
    assert len(row["nested"]["blob"]) == MAX_VALUE_CHARS
    assert verify(path) == (True, 1)


def test_non_ascii_is_preserved(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append({"event": "allow", "note": "città, perché"})
    assert read_lines(path)[0]["note"] == "città, perché"
    assert verify(path) == (True, 1)


def test_creates_parent_directory(tmp_path) -> None:
    path = tmp_path / "guard" / "audit.jsonl"
    AuditLog(path).append({"event": "loaded"})
    assert path.exists()
