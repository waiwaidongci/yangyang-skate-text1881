import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src import models  # noqa: F401  register every table before create_all
    from src.config.database import Base
    from src.domain.scheduling import CAPACITY_REASON
    from src.models.account import Account
    from src.models.dataset import Dataset
    from src.models.run import Run
    from src.services import dispatch as dispatch_service

    AVAILABLE = True
except ImportError:
    AVAILABLE = False

SCORES = {
    'T-01': {'blade': 0.90, 'boot': 0.06, 'fastener': 0.04},
    'T-02': {'blade': 0.60, 'boot': 0.30, 'fastener': 0.10},
    'T-03': {'blade': 0.40, 'boot': 0.55, 'fastener': 0.05},
    'T-11': {'blade': 0.95, 'boot': 0.03, 'fastener': 0.02},
    'T-12': {'blade': 0.20, 'boot': 0.70, 'fastener': 0.10},
}


def fake_scores(run, dataset, tickets):
    return [SimpleNamespace(ticket=ticket, probabilities=SCORES[ticket['id']], matched_ngrams=3, gap=0.3)
            for ticket in sorted(tickets, key=lambda row: row['id'])]


def pending(ticket_id, shift='SR-P01'):
    return {'id': ticket_id, 'shift_id': shift, 'split': 'pending',
            'message': '柜台报修 ' + ticket_id, 'fault_label': None}


@unittest.skipUnless(AVAILABLE, '需要完整后端依赖（fastapi、sqlalchemy、scikit-learn）')
class DispatchFlowTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine, expire_on_commit=False)()
        self.actor = SimpleNamespace(id=1, role='admin')
        self.session.add(Account(id=1, email='admin@demo.local', password_hash='x', role='admin'))
        self.session.commit()
        patcher = mock.patch.object(dispatch_service, 'score_pending_tickets', fake_scores)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.session.close)

    def tearDown(self):
        self.session.rollback()

    def add_run(self, name, rows, threshold=0.5):
        dataset = Dataset(name=name, checksum=name, rows=rows, created_by=1)
        self.session.add(dataset)
        self.session.flush()
        run = Run(label=name, dataset_id=dataset.id, created_by=1, created_at='2026-11-27T20:00:00+00:00',
                  status='completed', settings={'alpha': 1.0, 'confidence_threshold': threshold},
                  result=None, error=None)
        self.session.add(run)
        self.session.commit()
        return run

    def create_batch(self, run, counter):
        payload = SimpleNamespace(run_id=run.id, shift_id='SR-P01', counter=counter)
        return dispatch_service.create_batch(self.session, payload, self.actor)

    def claim(self, ticket_id, station, revision):
        payload = SimpleNamespace(ticket_id=ticket_id, station=station, revision=revision)
        return dispatch_service.claim(self.session, 'SR-P01', payload, self.actor)

    def states(self, board):
        return {order['ticket_id']: (order['state'], order['station']) for order in board['orders']}

    def prepare_first_batch(self):
        run = self.add_run('first.csv', [pending('T-01'), pending('T-02'), pending('T-03')])
        dispatch_service.put_quota(self.session, 'SR-P01',
                                   SimpleNamespace(blade=1, boot=1, fastener=1), self.actor)
        batch = self.create_batch(run, '东柜')
        return dispatch_service.approve_batch(self.session, batch['id'], self.actor)

    def test_approve_schedules_within_shared_quota(self):
        board = self.prepare_first_batch()
        self.assertEqual(board['revision'], 1)
        self.assertEqual(self.states(board), {
            'T-01': ('assigned', 'blade'),
            'T-02': ('manual', None),
            'T-03': ('assigned', 'boot'),
        })
        reasons = {order['ticket_id']: order['reason'] for order in board['orders']}
        self.assertEqual(reasons['T-02'], CAPACITY_REASON)
        self.assertEqual(board['remaining'], {'blade': 0, 'boot': 0, 'fastener': 1})

    def test_new_batch_reschedules_unclaimed_and_keeps_claimed(self):
        self.prepare_first_batch()
        first = self.claim('T-01', 'blade', 1)
        self.assertEqual(first['state'], 'claimed')
        retry = self.claim('T-01', 'blade', 1)
        self.assertEqual(retry['state'], 'claimed')
        board = dispatch_service.board_view(self.session, 'SR-P01')
        self.assertEqual(board['claimed']['blade'], 1)

        run2 = self.add_run('second.csv', [pending('T-11'), pending('T-12')])
        batch2 = self.create_batch(run2, '西柜')
        board = dispatch_service.approve_batch(self.session, batch2['id'], self.actor)
        self.assertEqual(board['revision'], 2)
        self.assertEqual(self.states(board), {
            'T-01': ('claimed', 'blade'),
            'T-02': ('manual', None),
            'T-03': ('manual', None),
            'T-11': ('manual', None),
            'T-12': ('assigned', 'boot'),
        })
        self.assertEqual(board['claimed']['blade'], 1)
        self.assertEqual(board['remaining']['blade'], 0)
        self.assertEqual({batch['counter'] for batch in board['batches']}, {'东柜', '西柜'})

        with self.assertRaises(HTTPException) as stale:
            self.claim('T-12', 'boot', 1)
        self.assertEqual(stale.exception.status_code, 409)
        claimed = self.claim('T-12', 'boot', 2)
        self.assertEqual(claimed['state'], 'claimed')
        self.assertEqual(claimed['revision'], 2)

    def test_redispatched_order_rejects_old_list_claim(self):
        self.prepare_first_batch()
        run2 = self.add_run('second.csv', [pending('T-11'), pending('T-12')])
        batch2 = self.create_batch(run2, '西柜')
        board = dispatch_service.approve_batch(self.session, batch2['id'], self.actor)
        self.assertEqual(self.states(board)['T-03'], ('manual', None))
        with self.assertRaises(HTTPException) as stale_revision:
            self.claim('T-03', 'boot', 1)
        self.assertEqual(stale_revision.exception.status_code, 409)
        with self.assertRaises(HTTPException) as moved:
            self.claim('T-03', 'boot', 2)
        self.assertEqual(moved.exception.status_code, 409)

    def test_claim_validation(self):
        self.prepare_first_batch()
        with self.assertRaises(LookupError):
            self.claim('T-99', 'blade', 1)
        with self.assertRaises(HTTPException):
            self.claim('T-01', 'boot', 1)
        with self.assertRaises(HTTPException):
            self.claim('T-02', 'blade', 1)
        self.claim('T-01', 'blade', 1)
        with self.assertRaises(HTTPException) as taken:
            self.claim('T-01', 'boot', 1)
        self.assertEqual(taken.exception.status_code, 409)

    def test_double_approve_rejected(self):
        run = self.add_run('first.csv', [pending('T-01')])
        batch = self.create_batch(run, '东柜')
        dispatch_service.approve_batch(self.session, batch['id'], self.actor)
        with self.assertRaises(HTTPException) as again:
            dispatch_service.approve_batch(self.session, batch['id'], self.actor)
        self.assertEqual(again.exception.status_code, 409)

    def test_batch_creation_validation(self):
        run = self.add_run('first.csv', [pending('T-01')])
        batch = self.create_batch(run, '东柜')
        self.assertEqual(batch['state'], 'submitted')
        self.assertEqual(batch['tickets'], 1)
        with self.assertRaises(HTTPException):
            self.create_batch(run, '西柜')
        run2 = self.add_run('second.csv', [pending('T-01')])
        with self.assertRaises(HTTPException) as overlap:
            self.create_batch(run2, '西柜')
        self.assertEqual(overlap.exception.status_code, 409)
        run3 = self.add_run('third.csv', [pending('T-31', shift='SR-P02')])
        with self.assertRaises(ValueError):
            self.create_batch(run3, '西柜')
        with self.assertRaises(LookupError):
            dispatch_service.create_batch(
                self.session, SimpleNamespace(run_id=9999, shift_id='SR-P01', counter='西柜'), self.actor)

    def test_unfinished_run_cannot_batch(self):
        dataset = Dataset(name='raw.csv', checksum='raw', rows=[pending('T-01')], created_by=1)
        self.session.add(dataset)
        self.session.flush()
        run = Run(label='raw', dataset_id=dataset.id, created_by=1, created_at='2026-11-27T20:00:00+00:00',
                  status='queued', settings={'alpha': 1.0, 'confidence_threshold': 0.5}, result=None, error=None)
        self.session.add(run)
        self.session.commit()
        with self.assertRaises(HTTPException) as not_ready:
            self.create_batch(run, '东柜')
        self.assertEqual(not_ready.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
