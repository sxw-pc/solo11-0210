import unittest

from courts_rain.db import connect
from courts_rain.intake_diagnostics import diagnose
from courts_rain.workbench import FIXTURES,apply,load


def records():
    return [dict(id='assign-a',op='assign',fixture='f1',court='west',start=115,official='ref-a'),
            dict(id='assign-b',op='assign',fixture='f2',court='west',start=115,official='ref-b'),
            dict(id='assign-c',op='assign',fixture='f2',court='annex',start=160,official='ref-b')]


class IntakeDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(connect(':memory:'))
        league = load(FIXTURES/'league.json')
        stop = next(i for i,row in enumerate(league) if row['id']=='old-f1')
        apply(self.db,league[:stop])

    def test_omission_changes_implicit_conflict_and_then_future_identity(self):
        report = diagnose(self.db, records(), [0,1])
        self.assertEqual([row['status'] for row in report['original']], ['accepted','rejected','accepted'])
        self.assertEqual([(x['row'],x['after']['status']) for x in report['alternatives'][0]['changes']],
                         [(0,'omitted'),(1,'accepted'),(2,'rejected')])
        self.assertEqual([x['row'] for x in report['alternatives'][1]['changes']],[1])
        self.assertEqual(self.db.execute('SELECT count(*) FROM assignments').fetchone()[0],0)

    def test_same_id_retransmission_is_distinct_by_record_position(self):
        repeated = [records()[0]] * 3
        report = diagnose(self.db,repeated,[0])
        self.assertEqual(report['original'][1]['receipt']['decision'],'duplicate')
        after = report['alternatives'][0]['changes']
        self.assertEqual(after[1]['after']['receipt']['decision'],'slot-assigned')
        self.assertEqual([x['row'] for x in after],[0,1])
