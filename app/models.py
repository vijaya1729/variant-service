from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Variant(BaseModel):
    key: str
    weight: int = Field(gt=0)
    payload: dict = {}


class Experiment(BaseModel):
    id: str
    salt: str
    traffic: int = Field(ge=0, le=100)
    variants: list[Variant] = Field(min_length=2)
    status: Literal["draft", "running", "paused", "stopped"]

    @field_validator("variants")
    @classmethod
    def check_variants(cls, variants: list[Variant]) -> list[Variant]:
        keys = [variant.key for variant in variants]
        if len(keys) != len(set(keys)):
            raise ValueError("variant keys must be unique")
        if sum(variant.weight for variant in variants) != 100:
            raise ValueError("variant weights must sum to 100")
        return variants
