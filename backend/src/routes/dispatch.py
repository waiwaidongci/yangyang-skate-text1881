from fastapi import APIRouter

from src.controllers.dependencies import Actor, Admin, Db, Writer
from src.schemas.dispatch import BatchCreate, ClaimCreate, QuotaUpdate
from src.services import dispatch

router = APIRouter(prefix='/dispatch')


@router.post('/batches', status_code=201)
def create_batch(payload: BatchCreate, session: Db, actor: Writer):
    return dispatch.create_batch(session, payload, actor)


@router.post('/batches/{batch_id}/approve')
def approve_batch(batch_id: int, session: Db, actor: Admin):
    return dispatch.approve_batch(session, batch_id, actor)


@router.get('/{shift_id}')
def board(shift_id: str, session: Db, actor: Actor):
    return dispatch.board_view(session, shift_id)


@router.put('/{shift_id}/quota')
def put_quota(shift_id: str, payload: QuotaUpdate, session: Db, actor: Admin):
    return dispatch.put_quota(session, shift_id, payload, actor)


@router.post('/{shift_id}/claims', status_code=201)
def claim(shift_id: str, payload: ClaimCreate, session: Db, actor: Writer):
    return dispatch.claim(session, shift_id, payload, actor)
