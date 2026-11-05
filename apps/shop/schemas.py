from datetime import datetime
from decimal import Decimal

from ninja import Schema

# ---- inputs ----


class CategoryIn(Schema):
    name: str
    order: int = 0
    is_active: bool = True


class CategoryUpdateIn(Schema):
    name: str | None = None
    order: int | None = None
    is_active: bool | None = None


class ProductIn(Schema):
    title: str
    summary: str = ""
    description: str = ""
    category_id: int | None = None
    product_type: str = "physical"
    price: Decimal = Decimal("0")
    compare_at_price: Decimal | None = None
    currency: str = "NGN"
    stock_quantity: int | None = None
    status: str = "draft"
    is_featured: bool = False
    badge: str = ""
    detail_label: str = ""


class ProductUpdateIn(Schema):
    title: str | None = None
    summary: str | None = None
    description: str | None = None
    category_id: int | None = None  # null explicitly = uncategorised
    product_type: str | None = None
    price: Decimal | None = None
    compare_at_price: Decimal | None = None  # null explicitly = clear
    currency: str | None = None
    stock_quantity: int | None = None  # null explicitly = unlimited
    status: str | None = None
    is_featured: bool | None = None
    badge: str | None = None
    detail_label: str | None = None


# ---- outputs ----


class CategoryOut(Schema):
    id: int
    name: str
    slug: str
    order: int
    is_active: bool


class CategoryAdminOut(CategoryOut):
    product_count: int = 0

    @staticmethod
    def resolve_product_count(obj):
        count = getattr(obj, "product_count", None)  # annotated on the list endpoint
        return count if count is not None else obj.products.count()


class ProductListOut(Schema):
    id: int
    title: str
    slug: str
    summary: str
    image: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    product_type: str
    price: float  # numeric in JSON, matching the course endpoints
    compare_at_price: float | None = None
    currency: str
    in_stock: bool
    is_featured: bool
    badge: str
    detail_label: str

    @staticmethod
    def resolve_image(obj):
        return obj.image.url if obj.image else None

    @staticmethod
    def resolve_category_name(obj):
        return obj.category.name if obj.category_id else None


class ProductDetailOut(ProductListOut):
    description: str


class ProductAdminOut(ProductDetailOut):
    status: str
    stock_quantity: int | None = None
    has_digital_file: bool = False

    @staticmethod
    def resolve_has_digital_file(obj):
        return bool(obj.digital_file)

    created_at: datetime
    updated_at: datetime


# ---------------- orders ----------------

class OrderLineIn(Schema):
    product_id: int
    quantity: int = 1


class DeliveryIn(Schema):
    name: str
    phone: str
    address: str
    city: str
    state: str


class OrderCreateIn(Schema):
    items: list[OrderLineIn]
    delivery: DeliveryIn | None = None  # required when the bag has a physical item


class PaystackInitOut(Schema):
    authorization_url: str
    reference: str


class OrderItemOut(Schema):
    id: int
    product_id: int | None = None
    title: str
    product_type: str
    unit_price: float
    quantity: int
    line_total: float
    downloadable: bool = False

    @staticmethod
    def resolve_line_total(obj):
        return float(obj.line_total)

    @staticmethod
    def resolve_downloadable(obj):
        return bool(
            obj.order.status == "paid"
            and obj.product_id
            and obj.product_type == "digital"
            and obj.product.digital_file
        )


class DeliveryOut(Schema):
    name: str
    phone: str
    address: str
    city: str
    state: str


class OrderOut(Schema):
    reference: str
    status: str
    fulfillment_status: str
    total: float
    currency: str
    payment_provider: str
    paid_at: datetime | None = None
    created_at: datetime
    requires_delivery: bool
    delivery: DeliveryOut | None = None
    manual_payment_status: str | None = None
    items: list[OrderItemOut]

    @staticmethod
    def resolve_total(obj):
        return float(obj.total)

    @staticmethod
    def resolve_items(obj):
        return list(obj.items.all())

    @staticmethod
    def resolve_delivery(obj):
        if not obj.delivery_address:
            return None
        return {
            "name": obj.delivery_name, "phone": obj.delivery_phone,
            "address": obj.delivery_address, "city": obj.delivery_city,
            "state": obj.delivery_state,
        }

    @staticmethod
    def resolve_manual_payment_status(obj):
        manual = getattr(obj, "manual_payment", None)
        return manual.status if manual else None


class OrderAdminOut(OrderOut):
    id: int
    user_id: int
    user_email: str
    admin_note: str
    manual_proof_url: str | None = None
    manual_bank_reference: str | None = None
    manual_review_note: str | None = None

    @staticmethod
    def resolve_user_email(obj):
        return obj.user.email

    @staticmethod
    def resolve_manual_proof_url(obj):
        manual = getattr(obj, "manual_payment", None)
        return manual.proof_file.url if manual else None

    @staticmethod
    def resolve_manual_bank_reference(obj):
        manual = getattr(obj, "manual_payment", None)
        return manual.bank_reference if manual else None

    @staticmethod
    def resolve_manual_review_note(obj):
        manual = getattr(obj, "manual_payment", None)
        return manual.review_note if manual else None


class OrderAdminUpdateIn(Schema):
    fulfillment_status: str | None = None
    admin_note: str | None = None


class ManualReviewIn(Schema):
    approve: bool
    note: str = ""


class RefundIn(Schema):
    restock: bool = False
