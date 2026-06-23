from typing import Any, Optional

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


class WorkspaceFieldPatch(BaseModel):
    route_name: Optional[str] = None
    owner_id: Optional[int] = None
    customer_note: Optional[str] = None
    butler: Optional[str] = None


class Et818AutofillTarget(BaseModel):
    token: str = ""
    add_url: str = ""


class Et818AutofillReport(BaseModel):
    ok: bool
    phase: str = ""
    order_id: int = 0
    order_no: str = ""
    token: str = ""
    add_url: str = ""
    template_name: str = ""
    transport_name: str = ""
    team_category: str = ""
    traveller_count: int = 0
    pickup_count: int = 0
    page_ready: bool = False
    required_main: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    detail: str = ""


class Et818AutofillActionPayload(BaseModel):
    token: str = ""
    add_url: str = ""


class Et818OrderLookup(BaseModel):
    order_no: str = Field(min_length=1)


class Et818OpenDetailPayload(BaseModel):
    reg_id: int
    biz_mode: Optional[int] = None
    plan_id: Optional[int] = 0


class VbkOrderLookupPayload(BaseModel):
    order_no: str = Field(min_length=1)
    list_kind: str = "auto"
    sync_if_missing: bool = True


class VbkDetailSyncPayload(BaseModel):
    order_type_text: str = ""
    confirm_status_text: str = ""
    payment_status_text: str = ""
    departure_date: str = ""
    return_date: str = ""
    departure_city: str = ""
    customer_name: str = ""
    customer_phone: str = ""
    distribution_channel: str = ""
    scenic_booking_no: str = ""
    reservation_scenic_name: str = ""
    merchant_note: str = ""
    raw_json: str = ""
    travellers: list[dict[str, Any]] = Field(default_factory=list)
    pickup_dropoff: list[dict[str, Any]] = Field(default_factory=list)


class DateParts(BaseModel):
    raw: str = ""
    year: str = ""
    month: str = ""
    day: str = ""


class TemplateMatchBasis(BaseModel):
    supplier_product_name: str = ""
    product_name: str = ""
    departure_city: str = ""
    channel: str = ""


class TemplateSelection(BaseModel):
    template_name: str = ""
    template_keyword: str = ""
    match_confidence: float = 0.0
    match_basis: TemplateMatchBasis
    needs_manual_confirm: bool = False


class RoomNeed(BaseModel):
    standard: int = 0
    big_bed: int = 0
    triple: int = 0
    single_female: int = 0
    single_male: int = 0
    nights: int = 0


class OrderInfo(BaseModel):
    adult_count: int = 0
    child_count: int = 0
    channel_name: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    departure_date: DateParts
    return_date: DateParts
    visit_date: Optional[DateParts] = None
    transport_name: str = ""
    order_no: str = ""
    sales_name: str = ""
    after_sales_name: str = ""
    team_category: str = ""
    scenic_booking_no: str = ""
    reservation_scenic_name: str = ""
    room_need: RoomNeed


class TravellerSource(BaseModel):
    encrypted_info_revealed: bool = False
    from_vbk_detail: bool = False


class Traveller(BaseModel):
    name: str = ""
    phone: str = ""
    id_type: str = ""
    id_no: str = ""
    gender: str = ""
    birth_date: DateParts
    age: int = 0
    person_type: str = ""
    native_place: str = ""
    note: str = ""
    source: TravellerSource


class PickupDropoffItem(BaseModel):
    action: str = ""
    date: DateParts
    location: str = ""
    flight_no: str = ""
    time: str = ""
    description: str = ""
    vehicle_company: str = ""
    driver_name: str = ""
    project_name: str = ""
    enabled: bool = True


class NotesBlock(BaseModel):
    order_note: str = ""
    merchant_note: str = ""
    internal_note: str = ""
    merged_note: str = ""


class TemplateAutofillExpected(BaseModel):
    product_section: bool = True
    ticket_section: bool = True
    room_section: bool = True


class MetaDebug(BaseModel):
    vbk_order_type: str = ""
    vbk_confirm_status: str = ""
    vbk_payment_status: str = ""


class MetaBlock(BaseModel):
    autofill_priority: list[str] = Field(default_factory=list)
    template_autofill_expected: TemplateAutofillExpected
    manual_review_recommended: bool = False
    warnings: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    debug: MetaDebug


class Et818Payload(BaseModel):
    template_selection: TemplateSelection
    order_info: OrderInfo
    travellers: list[Traveller] = Field(default_factory=list)
    pickup_dropoff: list[PickupDropoffItem] = Field(default_factory=list)
    notes: NotesBlock
    meta: MetaBlock


class Et818PayloadResponse(BaseModel):
    ok: bool
    source_platform: str = "VBK"
    order_id: int
    order_no: str
    total_amount_vbk: str = ""
    et818_payload: Et818Payload
