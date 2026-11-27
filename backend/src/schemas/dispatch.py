from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain.settings import STATIONS


class QuotaUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    blade: int = Field(ge=0, le=500)
    boot: int = Field(ge=0, le=500)
    fastener: int = Field(ge=0, le=500)


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    run_id: int = Field(gt=0)
    shift_id: str = Field(min_length=1, max_length=64)
    counter: str = Field(min_length=1, max_length=64)


class ClaimCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    ticket_id: str = Field(min_length=1, max_length=64)
    station: str
    revision: int = Field(ge=0)

    @field_validator('station')
    @classmethod
    def known_station(cls, value):
        if value not in STATIONS:
            raise ValueError('工位应为 ' + '、'.join(STATIONS))
        return value
