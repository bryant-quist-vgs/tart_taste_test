"""spool_test.py — the broker's access to a directory an agent controls.

Every test here plays the hostile agent: it arranges the spool so that a naive
broker would follow a link, block, read the wrong file, or write through to a
file outside the spool, and asserts that `spool.Spool` does none of those
(docs/protocol.md §4, decision D10).

Coverage:
  - Load-bearing: symbolic links at every level are refused
  - Load-bearing: hard links cannot be used to read or overwrite other files
  - a FIFO in place of a file does not block the broker
  - a directory in place of a file is an error, not a crash
  - names that are not request IDs are never touched
  - claim is exclusive with the client's withdrawal
  - listing: finished and foreign entries are ignored; the entry cap holds
  - a request directory that is a link is never looked through
  - opening leaks no descriptors, whatever it finds
  - a directory the broker may not search is an error, not a crash
  - a spool whose directories were replaced is no longer current
"""

import os
from pathlib import Path

import pytest
from conftest import World, open_spool

from tart_taste_test import checkouts, client_spool, protocol, spool

ID = "20261115T183000Z-3f9a1c2b4d5e"


def opened(world: World) -> spool.Spool:
    client_spool.ensure_spool(world.paths)
    result = open_spool(world.repo)
    assert result is not None
    return result


def descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def victim_file(tmp_path: Path) -> Path:
    victim = tmp_path / "operator-file"
    victim.write_text("precious\n")
    return victim


def test_checkout_without_a_spool_is_not_served(world: World) -> None:
    assert open_spool(world.repo) is None
    world.paths.base.mkdir()
    assert open_spool(world.repo) is None  # `.taste-test` alone is not a spool
    with pytest.raises(checkouts.CheckoutError):
        open_spool(world.repo / "missing")


# Load-bearing: a symbolic link at any level of the spool is refused.
@pytest.mark.parametrize("level", [".taste-test", "spool", "requests", "results"])
def test_symlinked_spool_directory_is_refused(world: World, tmp_path: Path, level: str) -> None:
    client_spool.ensure_spool(world.paths)
    paths = {
        ".taste-test": world.paths.base,
        "spool": world.paths.spool,
        "requests": world.paths.requests,
        "results": world.paths.results,
    }
    target = paths[level]
    moved = tmp_path / f"moved-{level}"
    target.rename(moved)
    os.symlink(moved, target)
    with pytest.raises(spool.SpoolError):
        open_spool(world.repo)


def test_symlinked_request_directory_is_refused(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    real = world.paths.request_dir(request_id)
    moved = tmp_path / "moved-request"
    real.rename(moved)
    os.symlink(moved, real)
    with pytest.raises(spool.SpoolError):
        box.claim(request_id)
    assert (moved / protocol.REQUEST_FILE).exists()  # nothing was renamed through the link


def test_symlinked_result_directory_is_not_written_through(world: World, tmp_path: Path) -> None:
    box = opened(world)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, world.paths.result_dir(ID))
    with pytest.raises(spool.SpoolError):
        box.write_document(ID, protocol.STATUS_FILE, {"schema": 1})
    with pytest.raises(spool.SpoolError):
        box.create_log(ID, protocol.OUTPUT_LOG)
    assert list(elsewhere.iterdir()) == []


# Load-bearing: a hard link cannot make the broker read another file.
def test_hard_linked_snapshot_is_refused(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    source.unlink()
    os.link(victim_file(tmp_path), source)
    with pytest.raises(spool.SpoolError, match="more than one link"):
        box.stage_source(request_id, tmp_path / "staged.tar", 1 << 20)


def test_symlinked_snapshot_is_refused(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    source.unlink()
    os.symlink(victim_file(tmp_path), source)
    with pytest.raises(spool.SpoolError):
        box.stage_source(request_id, tmp_path / "staged.tar", 1 << 20)
    assert not (tmp_path / "staged.tar").exists()


def test_snapshot_over_the_limit_is_refused(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    with pytest.raises(spool.SnapshotTooLarge, match="larger than 10 bytes"):
        box.stage_source(request_id, tmp_path / "staged.tar", 10)


def test_snapshot_is_copied_as_bytes_with_its_digest(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    digest, size = box.stage_source(request_id, tmp_path / "staged.tar", 1 << 20)
    assert (tmp_path / "staged.tar").read_bytes() == source.read_bytes()
    assert size == source.stat().st_size and len(digest) == 64
    assert (tmp_path / "staged.tar").stat().st_mode & 0o777 == 0o600
    box.remove_source(request_id)
    assert not source.exists()


# Load-bearing: a pre-created hard link is never written through.
@pytest.mark.parametrize("name", [protocol.OUTPUT_LOG, protocol.SETUP_LOG])
def test_log_precreated_as_a_hard_link_is_replaced_not_written(
    world: World, tmp_path: Path, name: str
) -> None:
    box = opened(world)
    victim = victim_file(tmp_path)
    world.paths.result_dir(ID).mkdir()
    os.link(victim, world.paths.result_dir(ID) / name)

    descriptor = box.create_log(ID, name)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(b"guest-controlled output\n")

    assert victim.read_text() == "precious\n"
    assert victim.stat().st_nlink == 1
    assert (world.paths.result_dir(ID) / name).read_bytes() == b"guest-controlled output\n"


@pytest.mark.parametrize("name", [protocol.STATUS_FILE, protocol.STATUS_FILE + protocol.TMP_SUFFIX])
def test_document_precreated_as_a_hard_link_is_replaced_not_written(
    world: World, tmp_path: Path, name: str
) -> None:
    box = opened(world)
    victim = victim_file(tmp_path)
    world.paths.result_dir(ID).mkdir()
    os.link(victim, world.paths.result_dir(ID) / name)
    box.write_document(ID, protocol.STATUS_FILE, protocol.status_document(ID, "queued", "", 1.0))
    assert victim.read_text() == "precious\n"
    written = (world.paths.result_dir(ID) / protocol.STATUS_FILE).read_bytes()
    assert protocol.parse_status(written).state == "queued"


def test_heartbeat_precreated_as_a_hard_link_is_replaced_not_written(
    world: World, tmp_path: Path
) -> None:
    box = opened(world)
    victim = victim_file(tmp_path)
    os.link(victim, world.paths.heartbeat)
    box.write_heartbeat(protocol.heartbeat_document(1, "0", 2.0, 0))
    assert victim.read_text() == "precious\n"


def test_directory_in_place_of_a_log_is_an_error(world: World) -> None:
    box = opened(world)
    (world.paths.result_dir(ID) / protocol.OUTPUT_LOG).mkdir(parents=True)
    with pytest.raises(spool.SpoolError):
        box.create_log(ID, protocol.OUTPUT_LOG)


def test_fifo_in_place_of_the_request_does_not_block(world: World) -> None:
    request_id = world.submit()
    box = opened(world)
    request_file = world.paths.request_dir(request_id) / protocol.REQUEST_FILE
    request_file.unlink()
    os.mkfifo(request_file)
    with pytest.raises(spool.SpoolError, match="not a regular file"):
        box.claim(request_id)  # returns at once; an ordinary open would wait for a writer


def test_fifo_in_place_of_the_snapshot_does_not_block(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    source = world.paths.request_dir(request_id) / protocol.SOURCE_FILE
    source.unlink()
    os.mkfifo(source)
    with pytest.raises(spool.SpoolError, match="not a regular file"):
        box.stage_source(request_id, tmp_path / "staged.tar", 1 << 20)


@pytest.mark.parametrize("name", ["..", "../..", "x/y", "", "20261115T183000Z-XYZ"])
def test_names_that_are_not_request_ids_are_never_used(world: World, name: str) -> None:
    box = opened(world)
    for action in (
        lambda: box.write_document(name, protocol.STATUS_FILE, {"schema": 1}),
        lambda: box.create_log(name, protocol.OUTPUT_LOG),
        lambda: box.claim(name),
    ):
        with pytest.raises(spool.SpoolError, match="not a request ID"):
            action()


def test_claim_returns_exactly_what_was_claimed_and_only_once(world: World) -> None:
    request_id = world.submit()
    box = opened(world)
    request_dir = world.paths.request_dir(request_id)
    original = (request_dir / protocol.REQUEST_FILE).read_bytes()

    claimed = box.claim(request_id)
    assert claimed == original
    # The agent recreates request.json after the claim: it is a new, unclaimed file.
    (request_dir / protocol.REQUEST_FILE).write_text("{}")
    assert (request_dir / protocol.CLAIMED_FILE).read_bytes() == original


def test_withdrawn_request_cannot_be_claimed_and_claimed_cannot_be_withdrawn(
    world: World,
) -> None:
    first = world.submit()
    second = world.submit()
    box = opened(world)

    assert client_spool.withdraw(world.paths, first) is True
    assert not world.paths.request_dir(first).exists()

    assert box.claim(second) is not None
    assert client_spool.withdraw(world.paths, second) is False
    assert world.paths.request_dir(second).exists()


def test_hard_linked_request_is_refused_after_claim(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    request_file = world.paths.request_dir(request_id) / protocol.REQUEST_FILE
    request_file.unlink()
    os.link(victim_file(tmp_path), request_file)
    with pytest.raises(spool.SpoolError, match="more than one link"):
        box.claim(request_id)


def test_listing_ignores_foreign_names_and_finished_requests(world: World) -> None:
    waiting = world.submit()
    finished = world.submit()
    box = opened(world)
    (world.paths.requests / "not-an-id").mkdir()
    (world.paths.requests / "README").write_text("x")
    box.write_document(finished, protocol.RESULT_FILE, {"schema": 1})

    assert box.unclaimed() == [waiting]
    assert box.acknowledged(waiting) is False
    box.write_document(waiting, protocol.STATUS_FILE, {"schema": 1})
    assert box.acknowledged(waiting) is True

    assert box.claim(waiting) is not None
    assert box.unclaimed() == []
    assert box.interrupted() == [waiting]


def test_entry_cap_bounds_one_scan(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    box = opened(world)
    monkeypatch.setattr(spool, "MAX_ENTRIES", 5)
    for index in range(20):
        directory = world.paths.requests / f"20261115T183000Z-{index:012x}"
        directory.mkdir()
        (directory / protocol.REQUEST_FILE).write_text("{}")
    assert len(box.unclaimed()) <= 5


def test_cancel_marker_and_request_time_are_visible(world: World) -> None:
    request_id = world.submit()
    box = opened(world)
    assert box.cancel_requested(request_id) is False
    (world.paths.request_dir(request_id) / protocol.CANCEL_FILE).touch()
    assert box.cancel_requested(request_id) is True
    modified = box.request_modified(request_id)
    assert modified is not None and modified > 0
    assert box.request_modified(ID) is None


def current(box: spool.Spool, root: Path) -> bool:
    top, _ = checkouts.open_path(root)
    try:
        return box.current(top)
    finally:
        os.close(top)


def test_removed_spool_is_no_longer_current(world: World, tmp_path: Path) -> None:
    box = opened(world)
    assert current(box, world.repo) is True
    world.paths.base.rename(tmp_path / "gone")
    assert current(box, world.repo) is False
    client_spool.ensure_spool(world.paths)  # a new spool at the same path is another spool
    assert current(box, world.repo) is False
    box.close()


@pytest.mark.parametrize("name", [protocol.REQUESTS_NAME, protocol.RESULTS_NAME])
def test_replaced_inner_directory_is_no_longer_current(
    world: World, tmp_path: Path, name: str
) -> None:
    box = opened(world)
    (world.paths.spool / name).rename(tmp_path / "moved")
    assert current(box, world.repo) is False
    (world.paths.spool / name).mkdir()
    assert current(box, world.repo) is False
    box.close()


def test_spool_reached_through_a_link_is_not_current(world: World, tmp_path: Path) -> None:
    box = opened(world)
    world.paths.base.rename(tmp_path / "moved")
    os.symlink(tmp_path / "moved", world.paths.base)
    assert current(box, world.repo) is False  # the same directories, but now behind a link
    box.close()


# A link in place of a request directory is never looked through, even to list it.
def test_linked_request_directory_is_not_listed(world: World, tmp_path: Path) -> None:
    request_id = world.submit()
    box = opened(world)
    real = world.paths.request_dir(request_id)
    (real / protocol.CANCEL_FILE).touch()
    moved = tmp_path / "moved-request"
    real.rename(moved)
    os.symlink(moved, real)
    assert box.unclaimed() == [] and box.interrupted() == []
    assert box.cancel_requested(request_id) is False
    assert box.request_modified(request_id) is None


def test_opening_a_spool_leaks_no_descriptors(world: World, tmp_path: Path) -> None:
    before = descriptors()
    assert open_spool(world.repo) is None  # no spool yet
    client_spool.ensure_spool(world.paths)
    opened(world).close()
    world.paths.results.rename(tmp_path / "moved")
    os.symlink(tmp_path / "moved", world.paths.results)
    for _ in range(20):
        with pytest.raises(spool.SpoolError):  # fails at the last directory
            open_spool(world.repo)
    assert descriptors() == before


@pytest.mark.parametrize("level", [".taste-test", "spool", "requests", "results"])
def test_directory_without_permission_is_an_error_not_a_crash(world: World, level: str) -> None:
    client_spool.ensure_spool(world.paths)
    target = {
        ".taste-test": world.paths.base,
        "spool": world.paths.spool,
        "requests": world.paths.requests,
        "results": world.paths.results,
    }[level]
    try:
        for mode in (0o000, 0o400):  # unreadable; readable but not searchable
            target.chmod(mode)
            with pytest.raises(spool.SpoolError):
                box = open_spool(world.repo)
                assert box is not None
                # A directory that can be read but not searched opens; using it fails.
                box.claim(ID)
                box.write_document(ID, protocol.STATUS_FILE, {})
    finally:
        target.chmod(0o700)
