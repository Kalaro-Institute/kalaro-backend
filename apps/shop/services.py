import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from ninja.errors import HttpError

from apps.notifications.services import notify

from .models import Product, ShopManualPayment, ShopOrder, ShopOrderItem

logger = logging.getLogger(__name__)

MAX_QTY_PER_ITEM = 50


# ---------------- stock ----------------

def _lock_products(product_ids):
    """Lock rows in a stable order so concurrent checkouts can't deadlock."""
    return {
        p.pk: p
        for p in Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk")
    }


def release_stock(order: ShopOrder) -> None:
    """Return reserved stock to the products. Idempotent."""
    if not order.stock_reserved:
        return
    items = list(order.items.exclude(product__isnull=True))
    products = _lock_products([i.product_id for i in items])
    for item in items:
        product = products.get(item.product_id)
        if product and product.stock_quantity is not None:
            product.stock_quantity += item.quantity
            product.save(update_fields=["stock_quantity", "updated_at"])
    order.stock_reserved = False
    order.save(update_fields=["stock_reserved", "updated_at"])


def _reserve_again(order: ShopOrder) -> bool:
    """Best-effort re-reserve for an order that was paid after being cancelled/expired.
    Returns False if any tracked product no longer has enough stock (nothing is taken
    in that case, so an admin can decide)."""
    items = list(order.items.exclude(product__isnull=True))
    products = _lock_products([i.product_id for i in items])
    for item in items:
        p = products.get(item.product_id)
        if p and p.stock_quantity is not None and p.stock_quantity < item.quantity:
            return False
    for item in items:
        p = products.get(item.product_id)
        if p and p.stock_quantity is not None:
            p.stock_quantity -= item.quantity
            p.save(update_fields=["stock_quantity", "updated_at"])
    order.stock_reserved = True
    order.save(update_fields=["stock_reserved", "updated_at"])
    return True


# ---------------- checkout ----------------

@transaction.atomic
def create_order(*, user, items: list[dict], delivery: dict | None) -> ShopOrder:
    """items: [{"product_id": int, "quantity": int}]"""
    if not items:
        raise HttpError(400, "Your bag is empty.")

    # merge duplicate lines
    wanted: dict[int, int] = {}
    for line in items:
        qty = line["quantity"]
        if qty < 1 or qty > MAX_QTY_PER_ITEM:
            raise HttpError(400, f"Quantity must be between 1 and {MAX_QTY_PER_ITEM}.")
        wanted[line["product_id"]] = wanted.get(line["product_id"], 0) + qty
    if any(q > MAX_QTY_PER_ITEM for q in wanted.values()):
        raise HttpError(400, f"Quantity per item cannot exceed {MAX_QTY_PER_ITEM}.")

    products = _lock_products(wanted.keys())
    problems = []
    for pid, qty in wanted.items():
        p = products.get(pid)
        if (
            not p
            or p.status != Product.Status.PUBLISHED
            or (p.category_id and not p.category.is_active)
        ):
            problems.append(f"Product {pid} is not available.")
        elif p.stock_quantity is not None and p.stock_quantity < qty:
            left = p.stock_quantity
            problems.append(
                f"{p.title}: only {left} left." if left else f"{p.title} is out of stock."
            )
    if problems:
        raise HttpError(409, " ".join(problems))

    currencies = {products[pid].currency for pid in wanted}
    if len(currencies) > 1:
        raise HttpError(400, "All items in one order must use the same currency.")

    needs_delivery = any(
        products[pid].product_type == Product.ProductType.PHYSICAL for pid in wanted
    )
    delivery_fields = {}
    if needs_delivery:
        if not delivery:
            raise HttpError(400, "Delivery details are required for physical items.")
        for key in ("name", "phone", "address", "city", "state"):
            value = (delivery.get(key) or "").strip()
            if not value:
                raise HttpError(400, f"Delivery {key} is required.")
            delivery_fields[f"delivery_{key}"] = value

    total = sum((products[pid].price * qty for pid, qty in wanted.items()), Decimal("0"))
    if total <= 0:
        raise HttpError(400, "This order has nothing to pay for.")

    order = ShopOrder.objects.create(
        user=user, reference=f"shop-{uuid.uuid4().hex[:16]}", total=total,
        currency=currencies.pop(), stock_reserved=True, **delivery_fields,
    )
    for pid, qty in wanted.items():
        p = products[pid]
        ShopOrderItem.objects.create(
            order=order, product=p, title=p.title, product_type=p.product_type,
            unit_price=p.price, quantity=qty,
        )
        if p.stock_quantity is not None:
            p.stock_quantity -= qty
            p.save(update_fields=["stock_quantity", "updated_at"])
    return order


# ---------------- payment outcomes ----------------

def _complete_fulfilment_for_digital(order: ShopOrder) -> None:
    if not order.requires_delivery:
        order.fulfillment_status = ShopOrder.Fulfillment.DELIVERED


@transaction.atomic
def mark_paid(order_id: int, *, provider: str, payload: dict | None = None) -> ShopOrder:
    """Idempotent: safe for replayed webhooks and double-clicked approvals."""
    order = ShopOrder.objects.select_for_update().get(pk=order_id)
    if order.status == ShopOrder.Status.PAID:
        return order

    note = ""
    if order.status in (ShopOrder.Status.CANCELLED, ShopOrder.Status.FAILED):
        # Money arrived for an order we had already released (e.g. expired).
        if not order.stock_reserved and not _reserve_again(order):
            note = "Paid after cancellation and stock was insufficient; needs manual review."

    order.status = ShopOrder.Status.PAID
    order.payment_provider = provider
    order.paid_at = timezone.now()
    order.provider_payload = payload or {}
    if note:
        order.admin_note = note
    _complete_fulfilment_for_digital(order)
    order.save()

    notify(
        user=order.user, type="payment_approved", title="Order confirmed",
        body=f"We received your payment for order {order.reference}.",
        link=f"/shop/orders/{order.reference}",
    )
    return order


def confirm_paystack_payment(reference: str, verified: dict) -> None:
    """Called from the shared Paystack webhook once the transaction is verified."""
    order = ShopOrder.objects.filter(reference=reference).first()
    if not order:
        return
    expected_kobo = int(order.total * 100)
    if verified.get("amount") != expected_kobo or verified.get("currency", order.currency) != (
        order.currency
    ):
        logger.error(
            "Shop order %s: Paystack amount/currency mismatch (got %s %s, expected %s %s)",
            reference, verified.get("amount"), verified.get("currency"),
            expected_kobo, order.currency,
        )
        return
    mark_paid(order.pk, provider=ShopOrder.Provider.PAYSTACK, payload=verified)


@transaction.atomic
def review_manual_payment(order_id: int, *, reviewer, approve: bool, note: str = "") -> ShopOrder:
    order = ShopOrder.objects.select_for_update().get(pk=order_id)
    manual = getattr(order, "manual_payment", None)
    if manual is None:
        raise HttpError(404, "This order has no manual payment to review.")
    if manual.status != ShopManualPayment.Status.PENDING_REVIEW:
        raise HttpError(409, "This payment has already been reviewed.")

    manual.reviewed_by = reviewer
    manual.reviewed_at = timezone.now()
    manual.review_note = note

    if approve:
        manual.status = ShopManualPayment.Status.APPROVED
        manual.save()
        return mark_paid(order.pk, provider=ShopOrder.Provider.MANUAL)

    manual.status = ShopManualPayment.Status.REJECTED
    manual.save()
    release_stock(order)
    order.status = ShopOrder.Status.FAILED
    order.save(update_fields=["status", "updated_at"])
    notify(
        user=order.user, type="payment_rejected", title="Payment could not be verified",
        body=f"We couldn't verify your payment for order {order.reference}. "
             f"Reason: {note or 'Please contact support.'}",
    )
    return order


# ---------------- cancel / refund / expiry ----------------

@transaction.atomic
def cancel_pending_order(order_id: int) -> ShopOrder:
    order = ShopOrder.objects.select_for_update().get(pk=order_id)
    if order.status != ShopOrder.Status.PENDING:
        raise HttpError(409, "Only orders awaiting payment can be cancelled.")
    manual = getattr(order, "manual_payment", None)
    if manual and manual.status == ShopManualPayment.Status.PENDING_REVIEW:
        raise HttpError(409, "A transfer is under review; it must be reviewed first.")
    release_stock(order)
    order.status = ShopOrder.Status.CANCELLED
    order.save(update_fields=["status", "updated_at"])
    return order


@transaction.atomic
def refund_order(order_id: int, *, restock: bool = False) -> ShopOrder:
    """Records a refund. The money itself is returned outside this system."""
    order = ShopOrder.objects.select_for_update().get(pk=order_id)
    if order.status != ShopOrder.Status.PAID:
        raise HttpError(409, "Only paid orders can be refunded.")
    if restock and not order.stock_reserved:
        raise HttpError(409, "This order's stock was already returned.")
    if restock:
        release_stock(order)
    order.status = ShopOrder.Status.REFUNDED
    order.save(update_fields=["status", "updated_at"])
    notify(
        user=order.user, type="general", title="Order refunded",
        body=f"Order {order.reference} has been marked as refunded.",
        link=f"/shop/orders/{order.reference}",
    )
    return order


def expire_stale_orders() -> int:
    """Cancel unpaid orders past the hold window so reserved stock is freed."""
    hold = timedelta(minutes=getattr(settings, "SHOP_ORDER_HOLD_MINUTES", 120))
    cutoff = timezone.now() - hold
    stale = ShopOrder.objects.filter(
        status=ShopOrder.Status.PENDING, created_at__lt=cutoff
    ).exclude(manual_payment__status=ShopManualPayment.Status.PENDING_REVIEW)
    count = 0
    for order_id in stale.values_list("pk", flat=True):
        try:
            cancel_pending_order(order_id)
            count += 1
        except HttpError:
            continue  # state changed under us; leave it alone
    return count
