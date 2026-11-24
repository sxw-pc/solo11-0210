"""Preview which incoming rows change verdict if one source receipt is removed."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from .db import connect, encode, integer, require
from .workbench import apply, load


def decisions(source, records, skip=None):
    outcomes = []
    with connect(':memory:') as scratch:
        source.backup(scratch)
        for index, row in enumerate(records):
            if index == skip:
                outcomes.append({'status': 'omitted'})
                continue
            try:
                result, = apply(scratch,[row])
                outcomes.append({'status': 'accepted', 'receipt': result})
            except (ValueError, sqlite3.Error) as exc:
                outcomes.append({'status': 'rejected', 'reason': str(exc)})
    return outcomes


def diagnose(source, records, suspects):
    require(isinstance(records, list), 'records must be a list')
    for index in suspects:
        require(integer(index, 'row index') < len(records), 'row index outside batch')
    changed = []
    with connect(':memory:') as baseline:
        source.backup(baseline)
        original = decisions(baseline, records)
        for index in suspects:
            alternative = decisions(baseline, records, index)
            changed.append({'omitted': index, 'changes': [
                {'row': i, 'before': before, 'after': after}
                for i, (before,after) in enumerate(zip(original,alternative)) if before != after]})
    return {'original': original, 'alternatives': changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('file', type=Path)
    parser.add_argument('rows', nargs='+', type=int)
    args = parser.parse_args(argv)
    try:
        with closing(sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True,
                                     isolation_level=None)) as source:
            result = diagnose(source,load(args.file),args.rows)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2,f'rejected: {exc}\n')
    print(encode(result))


if __name__ == '__main__':
    main()
