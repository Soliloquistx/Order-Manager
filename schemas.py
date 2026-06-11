from typing import Optional
from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    order_no: str = Field(min_length=1)
    external_order_no: str = ""
    product_name: str = Field(min_length=1)
    route_name: str = ""
    channel: str = Field(min_length=1)
    source_platform: str = ""
    customer_name: str = Field(min_length=1)
    customer_phone: str = ""
    backup_contact: str = ""
    customer_note: str = ""
    departure_date: str = Field(min_length=1)
    return_date: str = ""
    adult_count: int = 1
    child_count: int = 0
    room_count: Optional[int] = None
    total_amount: Optional[float] = None
    paid_amount: float = 0
    currency: str = "CNY"
    payment_status: str = "未支付"
    order_status: str = "待确认"
    follow_status: str = "待跟进"
    priority: str = "普通"
    owner_id: int = 1
    next_follow_up_at: str = ""
    initial_note: str = ""


class OrderUpdate(OrderCreate):
    pass


class NoteCreate(BaseModel):
    note_type: str = "普通备注"
    content: str = Field(min_length=1)
    follow_status_after: str = ""
    next_follow_up_at: str = ""
    created_by: int = 1


class StatusPatch(BaseModel):
    payment_status: Optional[str] = None
    order_status: Optional[str] = None
    follow_status: Optional[str] = None


class Et818OrderLookup(BaseModel):
    order_no: str = Field(min_length=1)


class Et818OpenDetailPayload(BaseModel):
    reg_id: int
    biz_mode: Optional[int] = None
    plan_id: Optional[int] = 0
