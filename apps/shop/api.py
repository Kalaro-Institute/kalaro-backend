import requests
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from ninja import File, Form, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.accounts.auth import admin, any_user
from apps.notifications.services import notify
from apps.payments.paystack import initialize_transaction

from . import services
from .models import Product, ProductCategory, ShopManualPayment, ShopOrder, ShopOrderItem
from .schemas import (
    CategoryAdminOut,
    CategoryIn,
    CategoryOut,
    CategoryUpdateIn,
    ManualReviewIn,
    OrderAdminOut,
    OrderAdminUpdateIn,
    OrderCreateIn,
    OrderOut,
    PaystackInitOut,
    ProductAdminOut,
    ProductDetailOut,
    ProductIn,
    ProductListOut,
    ProductUpdateIn,
    RefundIn,
)

router = Router(tags=["shop"])


def _validate_product_fields(data: dict):
    if "title" in data and not data["title"].strip():
        raise HttpError(400, "Title cannot be blank.")
    if "product_type" in data and data["product_type"] not in Product.ProductType.values:
        raise HttpError(
            400, f"product_type must be one of: {', '.join(Product.ProductType.values)}."
        )
    if "status" in data and data["status"] not in Product.Status.values:
        raise HttpError(400, f"status must be one of: {', '.join(Product.Status.values)}.")
    if data.get("price") is not None and data["price"] < 0:
        raise HttpError(400, "price cannot be negative.")
    if data.get("compare_at_price") is not None and data["compare_at_price"] < 0:
        raise HttpError(400, "compare_at_price cannot be negative.")
    if data.get("stock_quantity") is not None and data["stock_quantity"] < 0:
        raise HttpError(400, "stock_quantity cannot be negative.")
    if len(data.get("badge") or "") > 40:
        raise HttpError(400, "badge must be 40 characters or fewer.")
    if len(data.get("detail_label") or "") > 60:
        raise HttpError(400, "detail_label must be 60 characters or fewer.")
    if "currency" in data and len(data["currency"]) != 3:
        raise HttpError(400, "currency must be a 3-letter code.")
    if data.get("category_id") is not None and not ProductCategory.objects.filter(
        pk=data["category_id"]
    ).exists():
        raise HttpError(400, "That category does not exist.")


def _admin_products():
    return Product.objects.select_related("category")


# ---------------- public (shop page) ----------------

@router.get("/categories", response=list[CategoryOut], auth=None)
def list_categories(request):
    return ProductCategory.objects.filter(is_active=True)


@router.get("/products", response=list[ProductListOut], auth=None)
def list_products(
    request,
    category: str | None = None,  # category slug
    q: str | None = None,
    featured: bool | None = None,
    product_type: str | None = None,
    sort: str = "newest",  # newest | price_asc | price_desc
):
    qs = Product.objects.select_related("category").filter(status=Product.Status.PUBLISHED)
    qs = qs.filter(Q(category__isnull=True) | Q(category__is_active=True))
    if category:
        qs = qs.filter(category__slug=category)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(summary__icontains=q))
    if featured is not None:
        qs = qs.filter(is_featured=featured)
    if product_type:
        qs = qs.filter(product_type=product_type)
    ordering = {"newest": "-created_at", "price_asc": "price", "price_desc": "-price"}
    if sort not in ordering:
        raise HttpError(400, f"sort must be one of: {', '.join(ordering)}.")
    return qs.order_by(ordering[sort], "id")


@router.get("/products/{slug}", response=ProductDetailOut, auth=None)
def get_product(request, slug: str):
    return get_object_or_404(
        Product.objects.select_related("category"), slug=slug, status=Product.Status.PUBLISHED
    )


# ---------------- admin: categories ----------------

@router.get("/admin/categories", response=list[CategoryAdminOut], auth=admin)
def admin_list_categories(request):
    return ProductCategory.objects.annotate(product_count=Count("products"))


@router.post("/admin/categories", response={201: CategoryAdminOut}, auth=admin)
def admin_create_category(request, payload: CategoryIn):
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "Name cannot be blank.")
    if ProductCategory.objects.filter(name__iexact=name).exists():
        raise HttpError(409, "A category with that name already exists.")
    category = ProductCategory.objects.create(
        name=name, order=payload.order, is_active=payload.is_active
    )
    return 201, category


@router.patch("/admin/categories/{category_id}", response=CategoryAdminOut, auth=admin)
def admin_update_category(request, category_id: int, payload: CategoryUpdateIn):
    category = get_object_or_404(ProductCategory, pk=category_id)
    data = {k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None}
    if "name" in data:
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HttpError(400, "Name cannot be blank.")
        if ProductCategory.objects.filter(name__iexact=data["name"]).exclude(
            pk=category.pk
        ).exists():
            raise HttpError(409, "A category with that name already exists.")
    for field, value in data.items():
        setattr(category, field, value)
    category.save()
    return category


@router.delete("/admin/categories/{category_id}", response={200: dict}, auth=admin)
def admin_delete_category(request, category_id: int):
    category = get_object_or_404(ProductCategory, pk=category_id)
    category.delete()  # products become uncategorised (SET_NULL)
    return {"status": "deleted"}


# ---------------- admin: products ----------------

@router.get("/admin/products", response=list[ProductAdminOut], auth=admin)
def admin_list_products(
    request,
    status: str | None = None,
    category_id: int | None = None,
    q: str | None = None,
):
    qs = _admin_products()
    if status:
        qs = qs.filter(status=status)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(summary__icontains=q))
    return qs


@router.post("/admin/products", response={201: ProductAdminOut}, auth=admin)
def admin_create_product(request, payload: ProductIn):
    data = payload.dict()
    _validate_product_fields(data)
    product = Product.objects.create(**data)
    return 201, _admin_products().get(pk=product.pk)


@router.get("/admin/products/{product_id}", response=ProductAdminOut, auth=admin)
def admin_get_product(request, product_id: int):
    return get_object_or_404(_admin_products(), pk=product_id)


@router.patch("/admin/products/{product_id}", response=ProductAdminOut, auth=admin)
def admin_update_product(request, product_id: int, payload: ProductUpdateIn):
    product = get_object_or_404(Product, pk=product_id)
    data = payload.dict(exclude_unset=True)
    # These may be explicitly nulled; everything else ignores null.
    nullable = {"category_id", "compare_at_price", "stock_quantity"}
    data = {k: v for k, v in data.items() if v is not None or k in nullable}
    _validate_product_fields(data)
    for field, value in data.items():
        setattr(product, field, value)
    product.save()
    return _admin_products().get(pk=product.pk)


@router.delete("/admin/products/{product_id}", response={200: dict}, auth=admin)
def admin_delete_product(request, product_id: int):
    product = get_object_or_404(Product, pk=product_id)
    if product.image:
        product.image.delete(save=False)
    # Past buyers keep their order history, but the downloadable goes with the product.
    if product.digital_file:
        product.digital_file.delete(save=False)
    product.delete()
    return {"status": "deleted"}


@router.post("/admin/products/{product_id}/image", response=ProductAdminOut, auth=admin)
def admin_upload_product_image(request, product_id: int, image: UploadedFile = File(...)):
    product = get_object_or_404(Product, pk=product_id)
    if not (image.content_type or "").startswith("image/"):
        raise HttpError(400, "The uploaded file must be an image.")
    if product.image:
        product.image.delete(save=False)
    product.image = image
    product.save(update_fields=["image", "updated_at"])
    return _admin_products().get(pk=product.pk)


@router.post("/admin/products/{product_id}/digital-file", response=ProductAdminOut, auth=admin)
def admin_upload_digital_file(request, product_id: int, file: UploadedFile = File(...)):
    product = get_object_or_404(Product, pk=product_id)
    if product.product_type != Product.ProductType.DIGITAL:
        raise HttpError(400, "Only digital products can have a downloadable file.")
    if product.digital_file:
        product.digital_file.delete(save=False)
    product.digital_file = file
    product.save(update_fields=["digital_file", "updated_at"])
    return _admin_products().get(pk=product.pk)


# ---------------- customer: orders / checkout ----------------

def _orders_qs():
    return ShopOrder.objects.select_related("user", "manual_payment").prefetch_related(
        "items__product"
    )


def _my_order(request, reference: str) -> ShopOrder:
    return get_object_or_404(_orders_qs(), reference=reference, user=request.auth)


@router.post("/orders", response={201: OrderOut}, auth=any_user)
def create_order(request, payload: OrderCreateIn):
    order = services.create_order(
        user=request.auth,
        items=[line.dict() for line in payload.items],
        delivery=payload.delivery.dict() if payload.delivery else None,
    )
    return 201, _orders_qs().get(pk=order.pk)


@router.get("/orders", response=list[OrderOut], auth=any_user)
def my_orders(request):
    return _orders_qs().filter(user=request.auth)


@router.get("/orders/{reference}", response=OrderOut, auth=any_user)
def get_my_order(request, reference: str):
    return _my_order(request, reference)


@router.post("/orders/{reference}/paystack/initialize", response=PaystackInitOut, auth=any_user)
def initialize_paystack(request, reference: str):
    order = _my_order(request, reference)
    if order.status != ShopOrder.Status.PENDING:
        raise HttpError(409, "This order is not awaiting payment.")
    if hasattr(order, "manual_payment"):
        raise HttpError(409, "A bank transfer was already submitted for this order.")
    try:
        data = initialize_transaction(
            email=request.auth.email, amount_kobo=int(order.total * 100),
            reference=order.reference,
        )
    except (requests.RequestException, ValueError):
        raise HttpError(502, "Could not start the payment. Please try again.") from None
    return {"authorization_url": data["authorization_url"], "reference": order.reference}


@router.post("/orders/{reference}/manual", response=OrderOut, auth=any_user)
def submit_manual_payment(
    request, reference: str, bank_reference: str = Form(""), proof: UploadedFile = File(...)
):
    order = _my_order(request, reference)
    if order.status != ShopOrder.Status.PENDING:
        raise HttpError(409, "This order is not awaiting payment.")
    if hasattr(order, "manual_payment"):
        raise HttpError(409, "Proof of payment was already submitted for this order.")
    content_type = proof.content_type or ""
    if not (content_type.startswith("image/") or content_type == "application/pdf"):
        raise HttpError(400, "Proof must be an image or a PDF.")
    if proof.size > 10 * 1024 * 1024:
        raise HttpError(400, "Proof must be 10 MB or smaller.")
    ShopManualPayment.objects.create(
        order=order, proof_file=proof, bank_reference=bank_reference.strip()
    )
    return _orders_qs().get(pk=order.pk)


@router.post("/orders/{reference}/cancel", response=OrderOut, auth=any_user)
def cancel_my_order(request, reference: str):
    order = _my_order(request, reference)
    services.cancel_pending_order(order.pk)
    return _orders_qs().get(pk=order.pk)


@router.get("/orders/{reference}/items/{item_id}/download", auth=any_user)
def download_item(request, reference: str, item_id: int):
    order = _my_order(request, reference)
    if order.status != ShopOrder.Status.PAID:
        raise HttpError(403, "Downloads are available once the order is paid.")
    item = get_object_or_404(ShopOrderItem.objects.select_related("product"),
                             pk=item_id, order=order)
    product = item.product
    if not product or item.product_type != Product.ProductType.DIGITAL or not product.digital_file:
        raise HttpError(404, "There is nothing to download for this item.")
    filename = product.digital_file.name.rsplit("/", 1)[-1]
    return FileResponse(product.digital_file.open("rb"), as_attachment=True, filename=filename)


# ---------------- admin: orders ----------------

@router.get("/admin/orders", response=list[OrderAdminOut], auth=admin)
def admin_list_orders(
    request,
    status: str | None = None,
    fulfillment_status: str | None = None,
    awaiting_review: bool | None = None,
    q: str | None = None,  # reference or customer email
):
    qs = _orders_qs()
    if status:
        qs = qs.filter(status=status)
    if fulfillment_status:
        qs = qs.filter(fulfillment_status=fulfillment_status)
    if awaiting_review:
        qs = qs.filter(manual_payment__status=ShopManualPayment.Status.PENDING_REVIEW)
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(user__email__icontains=q))
    return qs


@router.get("/admin/orders/{order_id}", response=OrderAdminOut, auth=admin)
def admin_get_order(request, order_id: int):
    return get_object_or_404(_orders_qs(), pk=order_id)


@router.patch("/admin/orders/{order_id}", response=OrderAdminOut, auth=admin)
def admin_update_order(request, order_id: int, payload: OrderAdminUpdateIn):
    order = get_object_or_404(_orders_qs(), pk=order_id)
    data = {k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None}

    new_fulfillment = data.get("fulfillment_status")
    if new_fulfillment is not None:
        if new_fulfillment not in ShopOrder.Fulfillment.values:
            raise HttpError(
                400,
                "fulfillment_status must be one of: "
                f"{', '.join(ShopOrder.Fulfillment.values)}.",
            )
        if order.status != ShopOrder.Status.PAID:
            raise HttpError(409, "Only paid orders can be fulfilled.")
        if new_fulfillment != ShopOrder.Fulfillment.UNFULFILLED and not order.requires_delivery:
            raise HttpError(409, "Digital-only orders are delivered automatically.")
    if len(data.get("admin_note", "")) > 500:
        raise HttpError(400, "admin_note must be 500 characters or fewer.")

    changed_fulfillment = new_fulfillment and new_fulfillment != order.fulfillment_status
    for field, value in data.items():
        setattr(order, field, value)
    order.save()

    if changed_fulfillment and new_fulfillment in (
        ShopOrder.Fulfillment.SHIPPED, ShopOrder.Fulfillment.DELIVERED
    ):
        notify(
            user=order.user, type="general", title=f"Your order has been {new_fulfillment}",
            body=f"Order {order.reference} is now marked as {new_fulfillment}.",
            link=f"/shop/orders/{order.reference}",
        )
    return _orders_qs().get(pk=order.pk)


@router.post("/admin/orders/{order_id}/review-manual", response=OrderAdminOut, auth=admin)
def admin_review_manual_payment(request, order_id: int, payload: ManualReviewIn):
    get_object_or_404(ShopOrder, pk=order_id)
    services.review_manual_payment(
        order_id, reviewer=request.auth, approve=payload.approve, note=payload.note
    )
    return _orders_qs().get(pk=order_id)


@router.post("/admin/orders/{order_id}/cancel", response=OrderAdminOut, auth=admin)
def admin_cancel_order(request, order_id: int):
    get_object_or_404(ShopOrder, pk=order_id)
    services.cancel_pending_order(order_id)
    return _orders_qs().get(pk=order_id)


@router.post("/admin/orders/{order_id}/refund", response=OrderAdminOut, auth=admin)
def admin_refund_order(request, order_id: int, payload: RefundIn):
    get_object_or_404(ShopOrder, pk=order_id)
    services.refund_order(order_id, restock=payload.restock)
    return _orders_qs().get(pk=order_id)
