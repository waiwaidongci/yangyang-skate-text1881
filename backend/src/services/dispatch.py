from collections import Counter

from fastapi import HTTPException
from sqlalchemy import select

from src.constants.workflow import RunStatus
from src.domain.classifier import fit_routing_model
from src.domain.records import CODE
from src.domain.scheduling import PoolOrder, schedule
from src.domain.settings import DEFAULT_STATION_CAPACITY, STATIONS
from src.models.dispatch import DispatchBatch, DispatchBoard, DispatchOrder
from src.models.run import Run
from src.services.audit import record
from src.services.catalog import get_dataset
from src.utils.time import utc_stamp

BATCH_SUBMITTED = 'submitted'
BATCH_APPROVED = 'approved'
ORDER_STAGED = 'staged'
ORDER_ASSIGNED = 'assigned'
ORDER_MANUAL = 'manual'
ORDER_CLAIMED = 'claimed'


def normalize_shift_id(value):
    candidate = value.strip().upper()
    if not CODE.fullmatch(candidate):
        raise ValueError('交接班号须为 1 至 64 位编号，以英文字母或数字开头。')
    return candidate


def default_board(shift_id):
    return DispatchBoard(shift_id=shift_id, revision=0, blade=DEFAULT_STATION_CAPACITY,
                         boot=DEFAULT_STATION_CAPACITY, fastener=DEFAULT_STATION_CAPACITY, updated_at=None)


def get_board(session, shift_id, for_update=False):
    statement = select(DispatchBoard).where(DispatchBoard.shift_id == shift_id)
    if for_update:
        statement = statement.with_for_update()
    board = session.scalar(statement)
    if board is None:
        board = default_board(shift_id)
        board.updated_at = utc_stamp()
        session.add(board)
        session.flush()
    return board


def batch_info(batch):
    return {'id': batch.id, 'run_id': batch.run_id, 'shift_id': batch.shift_id,
            'counter': batch.counter, 'state': batch.state,
            'created_at': batch.created_at, 'approved_at': batch.approved_at}


def order_info(order):
    return {'ticket_id': order.ticket_id, 'batch_id': order.batch_id, 'state': order.state,
            'station': order.station, 'station_name': STATIONS.get(order.station),
            'scores': {label: round(score, 6) for label, score in order.scores.items()},
            'matched_ngrams': order.matched_ngrams, 'gap': round(order.gap, 6),
            'threshold': order.threshold, 'reason': order.reason,
            'claimed_by': order.claimed_by, 'claimed_at': order.claimed_at}


def assemble_board(session, board):
    orders = session.scalars(select(DispatchOrder).where(DispatchOrder.shift_id == board.shift_id)
                             .order_by(DispatchOrder.ticket_id)).all()
    batches = session.scalars(select(DispatchBatch).where(DispatchBatch.shift_id == board.shift_id)
                              .order_by(DispatchBatch.id)).all()
    claimed = Counter(order.station for order in orders if order.state == ORDER_CLAIMED)
    assigned = Counter(order.station for order in orders if order.state == ORDER_ASSIGNED)
    quota = {label: getattr(board, label) for label in STATIONS}
    return {
        'shift_id': board.shift_id,
        'revision': board.revision,
        'quota': quota,
        'claimed': {label: claimed.get(label, 0) for label in STATIONS},
        'assigned': {label: assigned.get(label, 0) for label in STATIONS},
        'remaining': {label: quota[label] - claimed.get(label, 0) - assigned.get(label, 0)
                      for label in STATIONS},
        'batches': [batch_info(batch) for batch in batches],
        'orders': [order_info(order) for order in orders],
    }


def board_view(session, shift_id):
    shift_id = normalize_shift_id(shift_id)
    board = session.scalar(select(DispatchBoard).where(DispatchBoard.shift_id == shift_id))
    return assemble_board(session, board or default_board(shift_id))


def put_quota(session, shift_id, payload, actor):
    board = get_board(session, normalize_shift_id(shift_id), for_update=True)
    for label in STATIONS:
        setattr(board, label, getattr(payload, label))
    board.updated_at = utc_stamp()
    record(session, 'dispatch.quota_updated', board.id, actor.id)
    session.commit()
    return assemble_board(session, board)


def score_pending_tickets(run, dataset, tickets):
    train = [row for row in dataset.rows if row['split'] == 'train']
    model = fit_routing_model(train, run.settings['alpha'])
    return model.score(sorted(tickets, key=lambda row: row['id']), run.settings['confidence_threshold'])


def create_batch(session, payload, actor):
    shift_id = normalize_shift_id(payload.shift_id)
    run = session.get(Run, payload.run_id)
    if run is None:
        raise LookupError('找不到运行')
    if run.status != RunStatus.COMPLETED:
        raise HTTPException(409, '运行尚未成功完成，不能生成报修批次')
    dataset = get_dataset(session, run.dataset_id)
    tickets = [row for row in dataset.rows if row['split'] == 'pending' and row['shift_id'] == shift_id]
    if not tickets:
        raise ValueError('该运行在此班次没有待修工单')
    if session.scalar(select(DispatchBatch.id).where(DispatchBatch.run_id == run.id,
                                                     DispatchBatch.shift_id == shift_id)):
        raise HTTPException(409, '该运行在此班次的报修批次已存在')
    taken = set(session.scalars(select(DispatchOrder.ticket_id).where(DispatchOrder.shift_id == shift_id)))
    overlap = taken & {row['id'] for row in tickets}
    if overlap:
        raise HTTPException(409, f"工单 {', '.join(sorted(overlap))} 已在本班其他批次")
    get_board(session, shift_id)
    batch = DispatchBatch(run_id=run.id, shift_id=shift_id, counter=payload.counter, state=BATCH_SUBMITTED,
                          created_by=actor.id, created_at=utc_stamp(), approved_at=None)
    session.add(batch)
    session.flush()
    predictions = score_pending_tickets(run, dataset, tickets)
    for prediction in predictions:
        session.add(DispatchOrder(
            batch_id=batch.id, shift_id=shift_id, ticket_id=prediction.ticket['id'],
            scores=prediction.probabilities, matched_ngrams=prediction.matched_ngrams,
            gap=prediction.gap, threshold=run.settings['confidence_threshold'],
            state=ORDER_STAGED, station=None, reason=None, claimed_by=None, claimed_at=None))
    record(session, 'dispatch.batch_created', batch.id, actor.id)
    session.commit()
    return {**batch_info(batch), 'tickets': len(predictions)}


def approve_batch(session, batch_id, actor):
    batch = session.get(DispatchBatch, batch_id)
    if batch is None:
        raise LookupError('找不到报修批次')
    board = get_board(session, batch.shift_id, for_update=True)
    if batch.state != BATCH_SUBMITTED:
        raise HTTPException(409, '批次已签核，不能重复签核')
    batch.state = BATCH_APPROVED
    batch.approved_at = utc_stamp()
    for order in session.scalars(select(DispatchOrder).where(DispatchOrder.batch_id == batch.id,
                                                            DispatchOrder.state == ORDER_STAGED)):
        order.state = ORDER_MANUAL
    pool = session.scalars(select(DispatchOrder).where(DispatchOrder.shift_id == batch.shift_id,
                                                       DispatchOrder.state.in_((ORDER_ASSIGNED, ORDER_MANUAL)))
                           .order_by(DispatchOrder.ticket_id)).all()
    pinned = Counter(order.station for order in session.scalars(
        select(DispatchOrder).where(DispatchOrder.shift_id == batch.shift_id,
                                    DispatchOrder.state == ORDER_CLAIMED)))
    capacities = {label: max(0, getattr(board, label) - pinned.get(label, 0)) for label in STATIONS}
    result = schedule([PoolOrder(ticket_id=order.ticket_id, scores=order.scores,
                                 matched_ngrams=order.matched_ngrams, gap=order.gap,
                                 threshold=order.threshold) for order in pool], capacities)
    for order in pool:
        if order.ticket_id in result.assignments:
            label, _ = result.assignments[order.ticket_id]
            order.state, order.station, order.reason = ORDER_ASSIGNED, label, None
        else:
            order.state, order.station, order.reason = ORDER_MANUAL, None, result.manual[order.ticket_id]
    board.revision += 1
    board.updated_at = utc_stamp()
    record(session, 'dispatch.batch_approved', batch.id, actor.id)
    session.commit()
    return assemble_board(session, board)


def claim(session, shift_id, payload, actor):
    board = get_board(session, normalize_shift_id(shift_id), for_update=True)
    order = session.scalar(select(DispatchOrder).where(
        DispatchOrder.shift_id == board.shift_id,
        DispatchOrder.ticket_id == payload.ticket_id.strip().upper()))
    if order is None:
        raise LookupError('找不到工单')
    if order.state == ORDER_CLAIMED:
        if order.station == payload.station:
            return {**order_info(order), 'revision': board.revision}
        raise HTTPException(409, '工单已被其他工位领取')
    if payload.revision != board.revision:
        raise HTTPException(409, '清单已更新，请按最新清单领取')
    if order.state != ORDER_ASSIGNED or order.station != payload.station:
        raise HTTPException(409, '工单未派往该工位或已被改派')
    order.state = ORDER_CLAIMED
    order.claimed_by = actor.id
    order.claimed_at = utc_stamp()
    record(session, 'dispatch.order_claimed', order.id, actor.id)
    session.commit()
    return {**order_info(order), 'revision': board.revision}
