import uuid

from django.db import transaction
from django.shortcuts import get_object_or_404
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.accounts.auth import admin, any_user
from apps.catalog.models import Course

from .models import ManualPayment, Order, Payment
from .paystack import initialize_transaction, verify_transaction, verify_webhook_signature
from .schemas import (
    EnrollmentOut,
    InitializePaymentIn,
    InitializePaymentOut,
    ManualReviewIn,
    OrderOut,
)
from .services import grant_enrollment
from apps.notifications.services import notify
from django.utils import timezone

router = Router(tags=["payments"])


# ---------------- Paystack flow ----------------

@router.post("/paystack/initialize", response=InitializePaymentOut, auth=any_user)
def paystack_initialize(request, payload: InitializePaymentIn):
    course = get_object_or_404(Course, pk=payload.course_id, status=Course.Status.PUBLISHED)
    user = request.auth

    reference = f"kalaro-{uuid.uuid4().hex[:16]}"
    order = Order.objects.create(
        user=user, course=course, amount=course.price, currency=course.currency,
        reference=reference,
    )

    data = initialize_transaction(
        email=user.email,
        amount_kobo=int(course.price * 100),
        reference=reference,
    )
    return {"authorization_url": data["authorization_url"], "reference": reference}


@router.post("/paystack/webhook", auth=None)
def paystack_webhook(request):
    signature = request.headers.get("x-paystack-signature", "")
    if not verify_webhook_signature(request.body, signature):
        raise HttpError(401, "Invalid signature.")

    import json
    event = json.loads(request.body)

    if event.get("event") != "charge.success":
        return {"status": "ignored"}

    reference = event["data"]["reference"]

    # Independent server-side verification — never trust the webhook body alone.
    verified = verify_transaction(reference)
    if verified["status"] != "success":
        return {"status": "not successful"}

    if reference.startswith("shop-"):
        from apps.shop.services import confirm_paystack_payment
        confirm_paystack_payment(reference, verified)
        return {"status": "ok"}

    _confirm_paystack_order(reference, verified)
    return {"status": "ok"}


@transaction.atomic
def _confirm_paystack_order(reference: str, verified: dict):
    order = Order.objects.select_for_update().filter(reference=reference).first()
    if not order:
        return  # unknown reference, nothing to do

    if order.status == Order.Status.PAID:
        return  # already processed — replayed webhook, no-op

    Payment.objects.get_or_create(
        provider_reference=reference,
        defaults={
            "order": order,
            "provider": Payment.Provider.PAYSTACK,
            "status": "success",
            "raw_payload": verified,
        },
    )
    order.status = Order.Status.PAID
    order.save(update_fields=["status"])

    grant_enrollment(user=order.user, course=order.course, source="paystack", order=order)


@router.get("/orders/{reference}/status", response=OrderOut, auth=any_user)
def order_status(request, reference: str):
    order = get_object_or_404(Order, reference=reference, user=request.auth)
    return order


# ---------------- manual bank transfer flow ----------------

@router.post("/manual/{course_id}/submit", response={201: OrderOut}, auth=any_user)
def submit_manual_payment(request, course_id: int, bank_reference: str = "",
                          proof: UploadedFile = File(...)):
    course = get_object_or_404(Course, pk=course_id, status=Course.Status.PUBLISHED)
    user = request.auth

    reference = f"manual-{uuid.uuid4().hex[:16]}"
    order = Order.objects.create(
        user=user, course=course, amount=course.price, currency=course.currency,
        reference=reference,
    )
    ManualPayment.objects.create(order=order, proof_file=proof, bank_reference=bank_reference)
    return 201, order


@router.get("/admin/manual-payments/pending", auth=admin)
def list_pending_manual_payments(request):
    return [
        {
            "order_id": mp.order_id,
            "reference": mp.order.reference,
            "user_email": mp.order.user.email,
            "course_title": mp.order.course.title,
            "amount": float(mp.order.amount),
            "proof_url": mp.proof_file.url,
            "bank_reference": mp.bank_reference,
            "submitted_at": mp.created_at.isoformat(),
        }
        for mp in ManualPayment.objects.filter(
            status=ManualPayment.Status.PENDING_REVIEW
        ).select_related("order__user", "order__course")
    ]


@router.post("/admin/manual-payments/{order_id}/review", auth=admin)
@transaction.atomic
def review_manual_payment(request, order_id: int, payload: ManualReviewIn):
    manual = get_object_or_404(
        ManualPayment.objects.select_related("order"), order_id=order_id
    )
    if manual.status != ManualPayment.Status.PENDING_REVIEW:
        raise HttpError(409, "This payment has already been reviewed.")

    manual.reviewed_by = request.auth
    manual.review_note = payload.note
    manual.reviewed_at = timezone.now()

    if payload.approve:
        manual.status = ManualPayment.Status.APPROVED
        manual.order.status = Order.Status.PAID
        manual.order.save(update_fields=["status"])
        grant_enrollment(
            user=manual.order.user, course=manual.order.course,
            source="manual", order=manual.order,
        )
        notify(
            user=manual.order.user, type="payment_approved",
            title="Payment approved",
            body=f"Your payment for {manual.order.course.title} has been approved. You now have access.",
            link=f"/courses/{manual.order.course.slug}",
        )
    else:
        manual.status = ManualPayment.Status.REJECTED
        manual.order.status = Order.Status.FAILED
        manual.order.save(update_fields=["status"])
        notify(
            user=manual.order.user, type="payment_rejected",
            title="Payment could not be verified",
            body=f"We couldn't verify your payment for {manual.order.course.title}. "
                 f"Reason: {payload.note or 'Please contact support.'}",
        )

    manual.save()
    return {"status": manual.status}


# ---------------- enrollments ----------------

@router.get("/my-enrollments", response=list[EnrollmentOut], auth=any_user)
def my_enrollments(request):
    return request.auth.enrollments.filter(status="active")