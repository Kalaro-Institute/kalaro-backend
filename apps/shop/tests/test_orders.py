import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from apps.accounts.models import Role, User
from apps.shop import services
from apps.shop.models import Product, ShopManualPayment, ShopOrder

pytestmark = pytest.mark.django_db

API = "/api/v1/shop"
DELIVERY = {"name": "Ada", "phone": "08012345678", "address": "1 Allen Ave",
            "city": "Ikeja", "state": "Lagos"}
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7"
       b"5\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")


def auth(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def call(method, user, url, body=None):
    kwargs = auth(user) if user else {}
    c = Client()
    if body is None:
        return getattr(c, method)(url, **kwargs)
    return getattr(c, method)(url, data=body, content_type="application/json", **kwargs)


@pytest.fixture(autouse=True)
def stub_side_effects(monkeypatch, settings, tmp_path):
    """No Celery/email, no real Paystack, files into a temp dir."""
    calls = []
    monkeypatch.setattr("apps.shop.services.notify", lambda **kw: calls.append(kw))
    monkeypatch.setattr("apps.shop.api.notify", lambda **kw: calls.append(kw))
    settings.MEDIA_ROOT = tmp_path
    settings.PAYSTACK_SECRET_KEY = "sk_test_x"
    return calls


@pytest.fixture
def notifications(stub_side_effects):
    return stub_side_effects


@pytest.fixture
def buyer():
    return User.objects.create_user(email="b@test.com", password="supersecret123",
                                    role=Role.STUDENT, email_verified=True)


@pytest.fixture
def other_buyer():
    return User.objects.create_user(email="b2@test.com", password="supersecret123",
                                    role=Role.STUDENT, email_verified=True)


@pytest.fixture
def admin_user():
    return User.objects.create_user(email="a@test.com", password="supersecret123",
                                    role=Role.ADMIN, email_verified=True)


@pytest.fixture
def book():
    return Product.objects.create(title="Handbook", price=69750, status="published",
                                  stock_quantity=5)


@pytest.fixture
def ebook():
    p = Product.objects.create(title="Template Pack", price=5000, status="published",
                               product_type="digital")
    p.digital_file.save("pack.txt", SimpleUploadedFile("pack.txt", b"secret-content"))
    return p


def place(user, items, delivery=DELIVERY):
    return call("post", user, f"{API}/orders", {"items": items, "delivery": delivery})


def paystack_event(reference, amount_kobo, currency="NGN"):
    body = json.dumps({"event": "charge.success", "data": {"reference": reference}}).encode()
    sig = hmac.new(b"sk_test_x", body, hashlib.sha512).hexdigest()
    return body, sig, {"status": "success", "amount": amount_kobo, "currency": currency}


def fire_webhook(monkeypatch, reference, amount_kobo, currency="NGN"):
    body, sig, verified = paystack_event(reference, amount_kobo, currency)
    monkeypatch.setattr("apps.payments.api.verify_transaction", lambda ref: verified)
    return Client().post("/api/v1/payments/paystack/webhook", data=body,
                         content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)


# ---------------- checkout ----------------

def test_create_order_reserves_stock_and_totals(buyer, book):
    r = place(buyer, [{"product_id": book.id, "quantity": 2}])
    assert r.status_code == 201
    body = r.json()
    assert body["reference"].startswith("shop-") and body["status"] == "pending"
    assert body["total"] == 139500.0 and body["requires_delivery"] is True
    assert body["items"][0]["line_total"] == 139500.0
    book.refresh_from_db()
    assert book.stock_quantity == 3


def test_duplicate_lines_are_merged(buyer, book):
    r = place(buyer, [{"product_id": book.id, "quantity": 1},
                      {"product_id": book.id, "quantity": 2}])
    assert r.status_code == 201 and len(r.json()["items"]) == 1
    assert r.json()["items"][0]["quantity"] == 3


@pytest.mark.parametrize("items", [
    [], [{"product_id": 1, "quantity": 0}], [{"product_id": 1, "quantity": 51}],
])
def test_bad_quantities_rejected(buyer, book, items):
    items = [dict(i, product_id=book.id) for i in items]
    assert place(buyer, items).status_code == 400


def test_out_of_stock_and_unavailable(buyer, book):
    assert place(buyer, [{"product_id": book.id, "quantity": 6}]).status_code == 409
    assert place(buyer, [{"product_id": 99999, "quantity": 1}]).status_code == 409
    draft = Product.objects.create(title="Draft", price=10)
    assert place(buyer, [{"product_id": draft.id, "quantity": 1}]).status_code == 409
    book.refresh_from_db()
    assert book.stock_quantity == 5  # nothing was taken by the failures


def test_physical_needs_delivery_digital_does_not(buyer, book, ebook):
    assert place(buyer, [{"product_id": book.id, "quantity": 1}], delivery=None).status_code == 400
    assert place(buyer, [{"product_id": book.id, "quantity": 1}],
                 delivery={**DELIVERY, "city": " "}).status_code == 400
    r = place(buyer, [{"product_id": ebook.id, "quantity": 1}], delivery=None)
    assert r.status_code == 201 and r.json()["requires_delivery"] is False


def test_mixed_currency_rejected(buyer, book):
    usd = Product.objects.create(title="Imported", price=10, currency="USD", status="published")
    r = place(buyer, [{"product_id": book.id, "quantity": 1},
                      {"product_id": usd.id, "quantity": 1}])
    assert r.status_code == 400


def test_checkout_requires_login(book):
    assert place(None, [{"product_id": book.id, "quantity": 1}]).status_code == 401


def test_orders_are_private(buyer, other_buyer, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    assert call("get", other_buyer, f"{API}/orders/{ref}").status_code == 404
    assert call("get", other_buyer, f"{API}/orders").json() == []
    assert len(call("get", buyer, f"{API}/orders").json()) == 1


def test_price_changes_do_not_alter_existing_order(buyer, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    book.price = 1
    book.title = "Renamed"
    book.save()
    body = call("get", buyer, f"{API}/orders/{ref}").json()
    assert body["total"] == 69750.0 and body["items"][0]["title"] == "Handbook"


# ---------------- cancel / expiry ----------------

def test_cancel_releases_stock_once(buyer, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 2}]).json()["reference"]
    r = call("post", buyer, f"{API}/orders/{ref}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    book.refresh_from_db()
    assert book.stock_quantity == 5
    assert call("post", buyer, f"{API}/orders/{ref}/cancel").status_code == 409
    book.refresh_from_db()
    assert book.stock_quantity == 5  # not double-credited


def test_expire_stale_orders(buyer, book):
    fresh = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    old = place(buyer, [{"product_id": book.id, "quantity": 2}]).json()["reference"]
    ShopOrder.objects.filter(reference=old).update(created_at=timezone.now() - timedelta(hours=3))
    assert services.expire_stale_orders() == 1
    assert ShopOrder.objects.get(reference=old).status == "cancelled"
    assert ShopOrder.objects.get(reference=fresh).status == "pending"
    book.refresh_from_db()
    assert book.stock_quantity == 4  # 5 - 1 (fresh still holds one)


# ---------------- Paystack ----------------

def test_paystack_initialize(buyer, book, monkeypatch):
    seen = {}

    def fake_init(**kw):
        seen.update(kw)
        return {"authorization_url": "https://paystack.test/pay"}

    monkeypatch.setattr("apps.shop.api.initialize_transaction", fake_init)
    ref = place(buyer, [{"product_id": book.id, "quantity": 2}]).json()["reference"]
    r = call("post", buyer, f"{API}/orders/{ref}/paystack/initialize")
    assert r.status_code == 200 and r.json()["authorization_url"] == "https://paystack.test/pay"
    assert seen["amount_kobo"] == 13950000 and seen["reference"] == ref


def test_paystack_initialize_rejected_when_not_pending(buyer, book, monkeypatch):
    monkeypatch.setattr("apps.shop.api.initialize_transaction", lambda **kw: {})
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    call("post", buyer, f"{API}/orders/{ref}/cancel")
    assert call("post", buyer, f"{API}/orders/{ref}/paystack/initialize").status_code == 409


def test_webhook_marks_paid_and_is_idempotent(buyer, book, monkeypatch, notifications):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    assert fire_webhook(monkeypatch, ref, 6975000).status_code == 200
    order = ShopOrder.objects.get(reference=ref)
    assert order.status == "paid" and order.payment_provider == "paystack" and order.paid_at
    sent = len(notifications)
    assert fire_webhook(monkeypatch, ref, 6975000).status_code == 200  # replay
    assert len(notifications) == sent  # no second confirmation
    book.refresh_from_db()
    assert book.stock_quantity == 4


def test_webhook_amount_mismatch_does_not_pay(buyer, book, monkeypatch):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    fire_webhook(monkeypatch, ref, 100)  # underpaid
    fire_webhook(monkeypatch, ref, 6975000, currency="USD")
    assert ShopOrder.objects.get(reference=ref).status == "pending"


def test_webhook_bad_signature_rejected(buyer, book):
    body = json.dumps({"event": "charge.success", "data": {"reference": "shop-x"}}).encode()
    r = Client().post("/api/v1/payments/paystack/webhook", data=body,
                      content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE="nope")
    assert r.status_code == 401


def test_webhook_unknown_shop_reference_is_harmless(monkeypatch):
    assert fire_webhook(monkeypatch, "shop-doesnotexist", 100).status_code == 200


def test_late_payment_after_expiry_still_pays_and_rereserves(buyer, book, monkeypatch):
    ref = place(buyer, [{"product_id": book.id, "quantity": 2}]).json()["reference"]
    ShopOrder.objects.filter(reference=ref).update(created_at=timezone.now() - timedelta(hours=3))
    services.expire_stale_orders()
    book.refresh_from_db()
    assert book.stock_quantity == 5
    fire_webhook(monkeypatch, ref, 13950000)
    order = ShopOrder.objects.get(reference=ref)
    book.refresh_from_db()
    assert order.status == "paid" and order.admin_note == "" and book.stock_quantity == 3


def test_late_payment_with_no_stock_left_is_flagged(buyer, other_buyer, book, monkeypatch):
    ref = place(buyer, [{"product_id": book.id, "quantity": 5}]).json()["reference"]
    call("post", buyer, f"{API}/orders/{ref}/cancel")
    place(other_buyer, [{"product_id": book.id, "quantity": 5}])  # takes it all
    fire_webhook(monkeypatch, ref, 5 * 6975000)
    order = ShopOrder.objects.get(reference=ref)
    assert order.status == "paid" and "manual review" in order.admin_note
    book.refresh_from_db()
    assert book.stock_quantity == 0  # never goes negative


def test_digital_only_order_is_delivered_on_payment(buyer, ebook, monkeypatch):
    ref = place(buyer, [{"product_id": ebook.id, "quantity": 1}], delivery=None).json()["reference"]
    fire_webhook(monkeypatch, ref, 500000)
    assert ShopOrder.objects.get(reference=ref).fulfillment_status == "delivered"


# ---------------- manual transfer ----------------

def submit_proof(user, ref, filename="p.png", content=PNG, ctype="image/png"):
    return Client().post(f"{API}/orders/{ref}/manual",
                         {"bank_reference": "TRX123",
                          "proof": SimpleUploadedFile(filename, content, ctype)}, **auth(user))


def test_manual_submit_and_admin_approve(buyer, admin_user, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    r = submit_proof(buyer, ref)
    assert r.status_code == 200 and r.json()["manual_payment_status"] == "pending_review"
    assert submit_proof(buyer, ref).status_code == 409  # one proof per order

    pending = call("get", admin_user, f"{API}/admin/orders?awaiting_review=true").json()
    assert [o["reference"] for o in pending] == [ref]
    assert pending[0]["manual_proof_url"] and pending[0]["manual_bank_reference"] == "TRX123"

    oid = pending[0]["id"]
    r = call("post", admin_user, f"{API}/admin/orders/{oid}/review-manual", {"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert r.json()["payment_provider"] == "manual"
    assert call("post", admin_user, f"{API}/admin/orders/{oid}/review-manual",
                {"approve": True}).status_code == 409


def test_manual_reject_fails_order_and_returns_stock(buyer, admin_user, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 3}]).json()["reference"]
    submit_proof(buyer, ref)
    oid = ShopOrder.objects.get(reference=ref).pk
    r = call("post", admin_user, f"{API}/admin/orders/{oid}/review-manual",
             {"approve": False, "note": "No matching credit"})
    assert r.json()["status"] == "failed"
    book.refresh_from_db()
    assert book.stock_quantity == 5


def test_manual_proof_validation(buyer, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    assert submit_proof(buyer, ref, "x.txt", b"hi", "text/plain").status_code == 400
    assert not ShopManualPayment.objects.exists()


def test_cannot_cancel_while_transfer_under_review(buyer, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    submit_proof(buyer, ref)
    assert call("post", buyer, f"{API}/orders/{ref}/cancel").status_code == 409
    # and the expiry job leaves it alone too
    ShopOrder.objects.filter(reference=ref).update(created_at=timezone.now() - timedelta(days=2))
    assert services.expire_stale_orders() == 0


def test_cannot_start_paystack_after_manual_submission(buyer, book, monkeypatch):
    monkeypatch.setattr("apps.shop.api.initialize_transaction", lambda **kw: {})
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    submit_proof(buyer, ref)
    assert call("post", buyer, f"{API}/orders/{ref}/paystack/initialize").status_code == 409


# ---------------- admin: fulfilment / refund / access ----------------

def paid_order(buyer, product, monkeypatch, qty=1):
    ref = place(buyer, [{"product_id": product.id, "quantity": qty}]).json()["reference"]
    fire_webhook(monkeypatch, ref, int(product.price * qty * 100))
    return ShopOrder.objects.get(reference=ref)


def test_admin_fulfilment_flow(buyer, admin_user, book, monkeypatch, notifications):
    order = paid_order(buyer, book, monkeypatch)
    url = f"{API}/admin/orders/{order.pk}"
    r = call("patch", admin_user, url, {"fulfillment_status": "shipped", "admin_note": "DHL 123"})
    assert r.status_code == 200 and r.json()["fulfillment_status"] == "shipped"
    assert r.json()["admin_note"] == "DHL 123"
    assert any("shipped" in n["title"] for n in notifications)
    assert call("patch", admin_user, url, {"fulfillment_status": "bogus"}).status_code == 400


def test_cannot_fulfil_unpaid_or_digital_orders(buyer, admin_user, book, ebook, monkeypatch):
    unpaid = ShopOrder.objects.get(reference=place(
        buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"])
    assert call("patch", admin_user, f"{API}/admin/orders/{unpaid.pk}",
                {"fulfillment_status": "shipped"}).status_code == 409
    digital = paid_order(buyer, ebook, monkeypatch)
    assert call("patch", admin_user, f"{API}/admin/orders/{digital.pk}",
                {"fulfillment_status": "shipped"}).status_code == 409


def test_admin_refund_with_and_without_restock(buyer, admin_user, book, monkeypatch):
    order = paid_order(buyer, book, monkeypatch, qty=2)
    book.refresh_from_db()
    assert book.stock_quantity == 3
    r = call("post", admin_user, f"{API}/admin/orders/{order.pk}/refund", {"restock": True})
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    book.refresh_from_db()
    assert book.stock_quantity == 5
    assert call("post", admin_user, f"{API}/admin/orders/{order.pk}/refund",
                {"restock": False}).status_code == 409  # already refunded


def test_refund_unpaid_rejected(buyer, admin_user, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    oid = ShopOrder.objects.get(reference=ref).pk
    assert call("post", admin_user, f"{API}/admin/orders/{oid}/refund",
                {"restock": False}).status_code == 409


def test_admin_cancel_pending(buyer, admin_user, book):
    ref = place(buyer, [{"product_id": book.id, "quantity": 1}]).json()["reference"]
    oid = ShopOrder.objects.get(reference=ref).pk
    r = call("post", admin_user, f"{API}/admin/orders/{oid}/cancel")
    assert r.json()["status"] == "cancelled"


def test_admin_list_filters(buyer, other_buyer, admin_user, book, monkeypatch):
    paid = paid_order(buyer, book, monkeypatch)
    place(other_buyer, [{"product_id": book.id, "quantity": 1}])
    listing = lambda qs: call("get", admin_user, f"{API}/admin/orders{qs}").json()  # noqa: E731
    assert len(listing("")) == 2
    assert [o["reference"] for o in listing("?status=paid")] == [paid.reference]
    assert [o["user_email"] for o in listing("?q=b2@")] == ["b2@test.com"]
    assert len(listing("?fulfillment_status=unfulfilled")) == 2


def test_order_admin_endpoints_forbidden_to_customers(buyer, book, monkeypatch):
    order = paid_order(buyer, book, monkeypatch)
    for method, path, body in [
        ("get", "/admin/orders", None),
        ("get", f"/admin/orders/{order.pk}", None),
        ("patch", f"/admin/orders/{order.pk}", {"admin_note": "x"}),
        ("post", f"/admin/orders/{order.pk}/refund", {"restock": False}),
        ("post", f"/admin/orders/{order.pk}/cancel", None),
        ("post", f"/admin/orders/{order.pk}/review-manual", {"approve": True}),
    ]:
        r = call(method, buyer, f"{API}{path}", body)
        assert r.status_code == 403, (method, path)


# ---------------- digital downloads ----------------

def test_download_only_when_paid_and_by_owner(buyer, other_buyer, ebook, monkeypatch):
    ref = place(buyer, [{"product_id": ebook.id, "quantity": 1}], delivery=None).json()["reference"]
    item_id = ShopOrder.objects.get(reference=ref).items.get().pk
    url = f"{API}/orders/{ref}/items/{item_id}/download"
    assert call("get", buyer, url).status_code == 403  # unpaid
    assert call("get", buyer, f"{API}/orders/{ref}").json()["items"][0]["downloadable"] is False

    fire_webhook(monkeypatch, ref, 500000)
    r = call("get", buyer, url)
    assert r.status_code == 200 and b"".join(r.streaming_content) == b"secret-content"
    assert call("get", buyer, f"{API}/orders/{ref}").json()["items"][0]["downloadable"] is True
    assert call("get", other_buyer, url).status_code == 404


def test_download_not_available_for_physical_item(buyer, book, monkeypatch):
    order = paid_order(buyer, book, monkeypatch)
    item = order.items.get()
    assert call("get", buyer,
                f"{API}/orders/{order.reference}/items/{item.pk}/download").status_code == 404


def test_digital_file_upload_admin_only_and_digital_only(admin_user, buyer, book, ebook):
    f = lambda: {"file": SimpleUploadedFile("a.pdf", b"%PDF", "application/pdf")}  # noqa: E731
    assert Client().post(f"{API}/admin/products/{ebook.id}/digital-file", f(),
                         **auth(buyer)).status_code == 403
    assert Client().post(f"{API}/admin/products/{book.id}/digital-file", f(),
                         **auth(admin_user)).status_code == 400
    r = Client().post(f"{API}/admin/products/{ebook.id}/digital-file", f(), **auth(admin_user))
    assert r.status_code == 200 and r.json()["has_digital_file"] is True
    assert "digital_file" not in r.json()  # the path is never exposed
