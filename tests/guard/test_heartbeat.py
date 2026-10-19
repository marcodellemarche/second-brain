import json

from second_brain.guard.heartbeat import is_fresh, read_heartbeat, write_heartbeat


def test_write_and_read(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"pid": 123, "plugin": "brain_guard"})
    beat = read_heartbeat(path)
    assert beat is not None
    assert beat["pid"] == 123
    assert beat["plugin"] == "brain_guard"
    assert isinstance(beat["ts"], float)


def test_write_creates_parent_directory(tmp_path) -> None:
    path = tmp_path / "guard" / "heartbeat.json"
    write_heartbeat(path, {"pid": 1})
    assert path.exists()


def test_write_leaves_no_temp_files(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"pid": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["heartbeat.json"]


def test_write_overwrites_in_place(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"pid": 1})
    write_heartbeat(path, {"pid": 2})
    beat = read_heartbeat(path)
    assert beat is not None
    assert beat["pid"] == 2


def test_ts_cannot_be_spoofed_by_info(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"ts": 1.0, "pid": 1})
    beat = read_heartbeat(path)
    assert beat is not None
    assert beat["ts"] != 1.0


def test_fresh(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"pid": 1})
    beat = read_heartbeat(path)
    assert beat is not None
    assert is_fresh(path, max_age=45, now=beat["ts"] + 10)


def test_stale(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, {"pid": 1})
    beat = read_heartbeat(path)
    assert beat is not None
    assert not is_fresh(path, max_age=45, now=beat["ts"] + 120)


def test_missing_file_is_not_fresh(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    assert read_heartbeat(path) is None
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0)


def test_corrupt_file_is_not_fresh(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_heartbeat(path) is None
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0)


def test_non_object_json_is_not_fresh(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_heartbeat(path) is None
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0)


def test_missing_or_bad_timestamp_is_not_fresh(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0)
    path.write_text(json.dumps({"ts": "yesterday"}), encoding="utf-8")
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0)
    path.write_text(json.dumps({"ts": True}), encoding="utf-8")
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0)


def test_future_timestamp_is_not_fresh(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"ts": 1_700_000_000.0}), encoding="utf-8")
    assert not is_fresh(path, max_age=45, now=1_700_000_000.0 - 600)
