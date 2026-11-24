from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from courts_rain.archive_audit import receipts
from courts_rain.db import connect
from courts_rain.workbench import FIXTURES, apply, audit, load


class ArchiveAuditTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory(dir=Path(__file__).parents[1])))

    def database(self, label, suffix=()):
        path = self.directory / label
        with connect(path) as db:
            apply(db, load(FIXTURES / 'league.json') + list(suffix))
        return path

    def test_each_archive_matches_its_own_cut_and_is_unchanged(self):
        published = self.database('published.db')
        recalled = self.database('recalled.db',load(FIXTURES / 'late-recall.json'))
        before = {path: path.read_bytes() for path in (published,recalled)}
        observed = receipts({'published':published, 'recalled':recalled})
        for label, path in [('published',published),('recalled',recalled)]:
            self.assertEqual(path.read_bytes(),before[path])
            with connect(path) as db:
                self.assertEqual(observed[label],audit(db))
        self.assertNotEqual(observed['published']['rounds'], observed['recalled']['rounds'])

    def test_an_archive_with_projection_drift_does_not_taint_good_archive(self):
        good = self.database('good.db')
        drift = self.database('drift.db')
        with connect(drift) as db:
            db.execute("UPDATE courts SET surface='porous' WHERE id='east'")
        reports = receipts({'good': good, 'drift': drift, 'missing': self.directory/'absent.db'})
        self.assertEqual(reports['good']['audit'],'verified')
        self.assertEqual(reports['drift']['drift'],['courts'])
        self.assertEqual(reports['missing']['audit'],'rejected')
