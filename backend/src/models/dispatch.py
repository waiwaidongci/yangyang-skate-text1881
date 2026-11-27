from sqlalchemy import JSON, Double, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base
from src.domain.settings import DEFAULT_STATION_CAPACITY


class DispatchBoard(Base):
    """One row per shift: station slots and the current dispatch list revision."""

    __tablename__ = 'dispatch_boards'

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[str] = mapped_column(String(64), unique=True)
    revision: Mapped[int] = mapped_column(default=0)
    blade: Mapped[int] = mapped_column(default=DEFAULT_STATION_CAPACITY)
    boot: Mapped[int] = mapped_column(default=DEFAULT_STATION_CAPACITY)
    fastener: Mapped[int] = mapped_column(default=DEFAULT_STATION_CAPACITY)
    updated_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DispatchBatch(Base):
    """A counter's repair batch for one shift, sourced from a completed run."""

    __tablename__ = 'dispatch_batches'
    __table_args__ = (UniqueConstraint('run_id', 'shift_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    shift_id: Mapped[str] = mapped_column(String(64), index=True)
    counter: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[str] = mapped_column(String(32))
    approved_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DispatchOrder(Base):
    """One repair ticket in a shift pool: staged, assigned, manual or claimed."""

    __tablename__ = 'dispatch_orders'
    __table_args__ = (UniqueConstraint('shift_id', 'ticket_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('dispatch_batches.id'), index=True)
    shift_id: Mapped[str] = mapped_column(String(64), index=True)
    ticket_id: Mapped[str] = mapped_column(String(64))
    scores: Mapped[dict] = mapped_column(JSON)
    matched_ngrams: Mapped[int] = mapped_column()
    gap: Mapped[float] = mapped_column(Double)
    threshold: Mapped[float] = mapped_column(Double)
    state: Mapped[str] = mapped_column(String(16), index=True)
    station: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[int | None] = mapped_column(ForeignKey('accounts.id'), nullable=True)
    claimed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
