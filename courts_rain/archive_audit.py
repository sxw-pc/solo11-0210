"""Audit a set of read-only backup copies before handoff."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from .db import encode, require
from .workbench import audit


def receipts(archives):
    results = {}
    for label, archive in archives.items():
        require(label and label not in results, 'unique archive label required')
        path = Path(archive).resolve()
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', isolation_level=None, uri=True)) as db:
                db.row_factory = sqlite3.Row
                results[label] = audit(db)
        except (ValueError, sqlite3.Error) as exc:
            results[label] = {'audit': 'rejected', 'reason': str(exc)}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', nargs='+', metavar='LABEL=ARCHIVE')
    args = parser.parse_args(argv)
    try:
        archives = {}
        for item in args.receipt:
            label, delimiter, path = item.partition('=')
            require(delimiter and label and path and label not in archives, 'expected unique LABEL=ARCHIVE')
            archives[label] = path
        output = receipts(archives)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f'rejected: {exc}\n')
    print(encode(output))
    return int(any(report['audit'] != 'verified' for report in output.values()))


if __name__ == '__main__':
    raise SystemExit(main())
