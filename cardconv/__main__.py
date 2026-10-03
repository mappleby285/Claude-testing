"""Command line: python -m cardconv {process,watch,export,status} ...

The browser interface (run_app.bat) is the main way to use the app; the command line
is handy for scheduled or unattended runs.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import api_key_configured, load_settings
from .pipeline import Pipeline
from .watcher import FolderWatcher, queue_folder


def _print_summary(pipeline: Pipeline) -> None:
    for key, value in pipeline.summary().items():
        print(f"  {key.replace('_', ' '):28s} {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cardconv", description="Business card to Outlook contacts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="process all images in a folder once")
    p.add_argument("folder", type=Path)
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--no-pairing", action="store_true", help="do not pair *_front/*_back files")
    w = sub.add_parser("watch", help="watch a folder and process new images until Ctrl+C")
    w.add_argument("folder", type=Path)
    w.add_argument("--recursive", action="store_true")
    w.add_argument("--no-pairing", action="store_true")
    sub.add_parser("export", help="export all approved contacts")
    sub.add_parser("status", help="show counts")
    m = sub.add_parser("mode", help="set export mode")
    m.add_argument("mode", choices=["review", "auto"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    pipeline = Pipeline(settings)

    if args.cmd in ("process", "watch") and not api_key_configured():
        print("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.", file=sys.stderr)
        return 2

    if args.cmd == "process":
        if not args.folder.is_dir():
            print(f"Folder not found: {args.folder}", file=sys.stderr)
            return 2
        results = queue_folder(pipeline, args.folder, args.recursive, not args.no_pairing)
        print(f"{len(results)} card(s) found; {sum(r.status == 'identical' for r in results)} already processed.")

        def progress(i: int, total: int, _card: str) -> None:
            if total:
                print(f"\r  processing {min(i + 1, total)}/{total}", end="", flush=True)

        summary = pipeline.process_queue(progress=progress)
        print()
        if summary.stopped_reason:
            print(f"Stopped: {summary.stopped_reason}")
        _print_summary(pipeline)
    elif args.cmd == "watch":
        watcher = FolderWatcher(pipeline, args.folder, args.recursive, not args.no_pairing)
        watcher.start()
        print(f"Watching {args.folder} (mode: {pipeline.export_mode}). Press Ctrl+C to stop.")
        try:
            while watcher.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        watcher.stop()
        _print_summary(pipeline)
    elif args.cmd == "export":
        result = pipeline.export_cards()
        print(f"Exported {result.count} contact(s) to {settings.export_dir}")
        for path in result.csvs.values():
            print(f"  {path}")
    elif args.cmd == "status":
        _print_summary(pipeline)
    elif args.cmd == "mode":
        pipeline.set_export_mode(args.mode)
        print(f"Export mode set to {args.mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
