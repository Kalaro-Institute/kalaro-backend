import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from apps.accounts.models import Role, User
from apps.shop.models import Product, ProductCategory

pytestmark = pytest.mark.django_db

API = "/api/v1/shop"


def auth(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def call(method, user, url, body=None):
    kwargs = auth(user) if user else {}
    c = Client()
    if body is None:
        return getattr(c, method)(url, **kwargs)
    return getattr(c, method)(url, data=body, content_type="application/json", **kwargs)


@pytest.fixture
def admin_user():
    return User.objects.create_user(email="a@test.com", password="supersecret123",
                                    role=Role.ADMIN, email_verified=True)


@pytest.fixture
def instructor_user():
    return User.objects.create_user(email="i@test.com", password="supersecret123",
                                    role=Role.INSTRUCTOR, email_verified=True)


@pytest.fixture
def category():
    return ProductCategory.objects.create(name="Merch")


def test_default_categories_seeded():
    names = list(ProductCategory.objects.filter(order__lt=4).values_list("name", flat=True))
    assert names[:4] == ["Books", "Templates", "Kits", "Apparel"]


@pytest.fixture
def published(category):
    return Product.objects.create(title="Kalaro Hoodie", price=15000, category=category,
                                  status=Product.Status.PUBLISHED, stock_quantity=5)


# ---- public ----

def test_public_lists_only_published(published):
    Product.objects.create(title="Hidden draft", price=1)
    r = call("get", None, f"{API}/products")
    assert r.status_code == 200
    assert [p["slug"] for p in r.json()] == ["kalaro-hoodie"]
    assert r.json()[0]["category_name"] == "Merch" and r.json()[0]["in_stock"] is True


def test_public_filters_and_sort(published, category):
    Product.objects.create(title="Mug", price=3000, category=category,
                           status=Product.Status.PUBLISHED, is_featured=True)
    Product.objects.create(title="Ebook", price=500, status=Product.Status.PUBLISHED,
                           product_type="digital")
    titles = lambda r: [p["title"] for p in r.json()]  # noqa: E731
    assert titles(call("get", None, f"{API}/products?category=merch&sort=price_asc")) == [
        "Mug", "Kalaro Hoodie"]
    assert titles(call("get", None, f"{API}/products?q=ebook")) == ["Ebook"]
    assert titles(call("get", None, f"{API}/products?featured=true")) == ["Mug"]
    assert titles(call("get", None, f"{API}/products?product_type=digital")) == ["Ebook"]
    assert call("get", None, f"{API}/products?sort=bogus").status_code == 400


def test_public_hides_inactive_category_products(published, category):
    category.is_active = False
    category.save()
    assert call("get", None, f"{API}/products").json() == []
    assert "merch" not in [c["slug"] for c in call("get", None, f"{API}/categories").json()]


def test_public_detail_and_draft_404(published):
    assert call("get", None, f"{API}/products/kalaro-hoodie").status_code == 200
    draft = Product.objects.create(title="Draft", price=1)
    assert call("get", None, f"{API}/products/{draft.slug}").status_code == 404


def test_out_of_stock_flag(published):
    published.stock_quantity = 0
    published.save()
    assert call("get", None, f"{API}/products").json()[0]["in_stock"] is False


# ---- admin auth ----

def test_admin_endpoints_require_admin(instructor_user, published):
    assert call("get", None, f"{API}/admin/products").status_code == 401
    assert call("get", instructor_user, f"{API}/admin/products").status_code == 403
    assert call("post", instructor_user, f"{API}/admin/products", {"title": "x"}).status_code == 403
    assert call("delete", instructor_user,
                f"{API}/admin/products/{published.id}").status_code == 403


# ---- admin categories ----

def test_admin_category_crud(admin_user):
    r = call("post", admin_user, f"{API}/admin/categories", {"name": "Stationery"})
    assert r.status_code == 201 and r.json()["slug"] == "stationery"
    cid = r.json()["id"]
    assert call("post", admin_user, f"{API}/admin/categories",
                {"name": "stationery"}).status_code == 409
    assert call("post", admin_user, f"{API}/admin/categories",
                {"name": "books"}).status_code == 409  # seeded default
    r = call("patch", admin_user, f"{API}/admin/categories/{cid}", {"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    listing = call("get", admin_user, f"{API}/admin/categories").json()
    assert next(c for c in listing if c["id"] == cid)["product_count"] == 0
    assert call("delete", admin_user, f"{API}/admin/categories/{cid}").status_code == 200
    assert not ProductCategory.objects.filter(pk=cid).exists()


def test_deleting_category_keeps_products(admin_user, published, category):
    call("delete", admin_user, f"{API}/admin/categories/{category.id}")
    published.refresh_from_db()
    assert published.category is None


# ---- admin products ----

def test_admin_create_product(admin_user, category):
    r = call("post", admin_user, f"{API}/admin/products", {
        "title": "Tote Bag", "price": "4500.00", "category_id": category.id,
        "status": "published", "stock_quantity": 20})
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "tote-bag" and body["status"] == "published"
    assert body["category_name"] == "Merch" and body["stock_quantity"] == 20


def test_admin_create_duplicate_title_gets_unique_slug(admin_user):
    for _ in range(2):
        call("post", admin_user, f"{API}/admin/products", {"title": "Same"})
    assert sorted(Product.objects.values_list("slug", flat=True)) == ["same", "same-2"]


@pytest.mark.parametrize("body", [
    {"title": " "}, {"title": "x", "price": "-1"}, {"title": "x", "status": "bogus"},
    {"title": "x", "product_type": "bogus"}, {"title": "x", "category_id": 9999},
    {"title": "x", "stock_quantity": -3},
])
def test_admin_create_validation(admin_user, body):
    assert call("post", admin_user, f"{API}/admin/products", body).status_code == 400


def test_admin_list_includes_drafts_and_filters(admin_user, published):
    Product.objects.create(title="Draft one", price=1)
    assert len(call("get", admin_user, f"{API}/admin/products").json()) == 2
    r = call("get", admin_user, f"{API}/admin/products?status=draft")
    assert [p["title"] for p in r.json()] == ["Draft one"]


def test_admin_update_product_partial_and_nulls(admin_user, published):
    r = call("patch", admin_user, f"{API}/admin/products/{published.id}",
             {"price": "999.50", "stock_quantity": None, "category_id": None})
    assert r.status_code == 200
    published.refresh_from_db()
    assert str(published.price) == "999.50"
    assert published.stock_quantity is None and published.category is None
    assert published.title == "Kalaro Hoodie"  # untouched
    # null on a non-nullable field is ignored, not an error
    assert call("patch", admin_user, f"{API}/admin/products/{published.id}",
                {"title": None}).status_code == 200
    published.refresh_from_db()
    assert published.title == "Kalaro Hoodie"


def test_admin_unpublish_removes_from_public(admin_user, published):
    call("patch", admin_user, f"{API}/admin/products/{published.id}", {"status": "archived"})
    assert call("get", None, f"{API}/products").json() == []


def test_admin_delete_product(admin_user, published):
    assert call("delete", admin_user, f"{API}/admin/products/{published.id}").status_code == 200
    assert not Product.objects.filter(pk=published.id).exists()
    assert call("delete", admin_user, f"{API}/admin/products/{published.id}").status_code == 404


def test_admin_upload_image(admin_user, published, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
           b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7"
           b"5\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
    url = f"{API}/admin/products/{published.id}/image"
    r = Client().post(url, {"image": SimpleUploadedFile("p.png", png, "image/png")},
                      **auth(admin_user))
    assert r.status_code == 200 and r.json()["image"]
    bad = Client().post(url, {"image": SimpleUploadedFile("p.txt", b"hi", "text/plain")},
                        **auth(admin_user))
    assert bad.status_code == 400


def test_badge_and_detail_label_roundtrip(admin_user, category):
    r = call("post", admin_user, f"{API}/admin/products", {
        "title": "HMO Operations Handbook", "price": "69750", "category_id": category.id,
        "status": "published", "badge": "Best seller", "detail_label": "296 pages"})
    assert r.status_code == 201
    pub = call("get", None, f"{API}/products").json()[0]
    assert pub["badge"] == "Best seller" and pub["detail_label"] == "296 pages"
    assert pub["price"] == 69750.0
    r = call("patch", admin_user, f"{API}/admin/products/{r.json()['id']}", {"badge": ""})
    assert r.json()["badge"] == "" and r.json()["detail_label"] == "296 pages"


def test_badge_too_long_rejected(admin_user):
    assert call("post", admin_user, f"{API}/admin/products",
                {"title": "x", "badge": "b" * 41}).status_code == 400
