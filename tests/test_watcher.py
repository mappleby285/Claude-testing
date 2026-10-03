from pathlib import Path

from cardconv import db
from cardconv.watcher import FolderWatcher, pair_by_name, queue_folder, split_side

from .conftest import extraction_dict, make_image, text_response


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_split_side():
    assert split_side(Path("Smith_Front.jpg")) == ("smith", "front")
    assert split_side(Path("smith-back.PNG")) == ("smith", "back")
    assert split_side(Path("smith back.webp")) == ("smith", "back")
    assert split_side(Path("front.jpg")) == ("front", None)
    assert split_side(Path("background.jpg")) == ("background", None)


def test_pair_by_name(tmp_path):
    names = ["a_front.jpg", "a_back.jpg", "b.jpg", "c_front.jpg"]
    paths = [tmp_path / n for n in names]
    groups = pair_by_name(paths)
    as_names = sorted([[p.name for p in g] for g in groups])
    assert as_names == [["a_front.jpg", "a_back.jpg"], ["b.jpg"], ["c_front.jpg"]]


def test_queue_folder_ignores_other_files_and_pairs(make_pipeline, tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a_front.png").write_bytes(make_image("af"))
    (folder / "a_back.png").write_bytes(make_image("ab"))
    (folder / "solo.jpg").write_bytes(make_image("s", fmt="JPEG"))
    (folder / "notes.txt").write_text("not an image")
    p, client = make_pipeline(default=text_response(extraction_dict()))
    results = queue_folder(p, folder)
    assert len(results) == 2
    summary = p.process_queue()
    assert summary.processed == 2 and len(client.calls) == 2
    # Running the folder again (e.g. after a restart) makes no new API calls
    again = queue_folder(p, folder)
    assert all(r.status == "identical" for r in again)
    p.process_queue()
    assert len(client.calls) == 2


def test_watch_waits_until_file_finished_copying(make_pipeline, tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    p, client = make_pipeline(default=text_response(extraction_dict()))
    clock = Clock()
    w = FolderWatcher(p, folder, clock=clock)
    full = make_image("growing", fmt="JPEG")
    target = folder / "card.jpg"

    target.write_bytes(full[: len(full) // 2])  # copy in progress
    assert w.scan_once() == 0
    clock.t += 5
    target.write_bytes(full)  # copy finished: size changed, so stability restarts
    assert w.scan_once() == 0
    clock.t += 1
    assert w.scan_once() == 0  # not yet stable for 2 s
    clock.t += 2
    assert w.scan_once() == 1
    assert len(client.calls) == 1
    clock.t += 10
    assert w.scan_once() == 0  # already handled
    assert len(client.calls) == 1


def test_watch_pairs_front_and_back_arriving_separately(make_pipeline, tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    p, client = make_pipeline(default=text_response(extraction_dict()))
    clock = Clock()
    w = FolderWatcher(p, folder, clock=clock)
    (folder / "x_front.png").write_bytes(make_image("front"))
    w.scan_once()
    clock.t += 3
    assert w.scan_once() == 0  # stable, but waiting for its back side
    (folder / "x_back.png").write_bytes(make_image("back"))
    w.scan_once()
    clock.t += 3
    assert w.scan_once() == 1
    cards = p.store.list_cards()
    assert len(cards) == 1 and len(cards[0].image_hashes) == 2


def test_watch_front_alone_after_pair_wait(make_pipeline, tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    p, _ = make_pipeline(default=text_response(extraction_dict()))
    clock = Clock()
    w = FolderWatcher(p, folder, clock=clock)
    (folder / "y_front.png").write_bytes(make_image("front"))
    w.scan_once()
    clock.t += 11  # pair wait is 10 s in the test settings
    assert w.scan_once() == 1


def test_watch_restart_does_not_reprocess(make_pipeline, tmp_path, settings):
    folder = tmp_path / "watch"
    folder.mkdir()
    (folder / "card.png").write_bytes(make_image("persist"))
    p, client = make_pipeline(default=text_response(extraction_dict()))
    clock = Clock()
    w = FolderWatcher(p, folder, clock=clock)
    w.scan_once()
    clock.t += 3
    w.scan_once()
    assert len(client.calls) == 1

    p2, client2 = make_pipeline(default=text_response(extraction_dict()))  # new process, same database
    w2 = FolderWatcher(p2, folder, clock=clock)
    w2.scan_once()
    clock.t += 3
    w2.scan_once()
    assert client2.calls == [] and w2.status.identical == 1


def test_watch_corrupt_file_recorded_after_grace_period(make_pipeline, tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    p, client = make_pipeline(default=text_response(extraction_dict()))
    clock = Clock()
    w = FolderWatcher(p, folder, clock=clock)
    (folder / "bad.jpg").write_bytes(b"\xff\xd8 truncated garbage")
    w.scan_once()
    clock.t += 3
    w.scan_once()  # stable but undecodable: wait a little longer
    assert p.store.list_cards() == []
    clock.t += 5
    w.scan_once()
    cards = p.store.list_cards()
    assert len(cards) == 1 and cards[0].status == db.FAILED
    assert client.calls == []


def test_watcher_thread_start_stop(make_pipeline, tmp_path):
    folder = tmp_path / "watch"
    folder.mkdir()
    p, _ = make_pipeline(default=text_response(extraction_dict()))
    w = FolderWatcher(p, folder, poll_seconds=0.05)
    w.start()
    assert w.running
    w.stop()
    assert not w.running
