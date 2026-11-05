import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel


def _unique_slug(model, base, pk):
    base = slugify(base) or "item"
    slug, n = base, 1
    while model.objects.filter(slug=slug).exclude(pk=pk).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


class ProductCategory(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]
        verbose_name_plural = "product categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(ProductCategory, self.name, self.pk)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Product(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")
        ARCHIVED = "archived", _("Archived")

    class ProductType(models.TextChoices):
        PHYSICAL = "physical", _("Physical")
        DIGITAL = "digital", _("Digital")

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    summary = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="shop_products/", blank=True, null=True)

    category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name="products")
    product_type = models.CharField(max_length=20, choices=ProductType.choices,
                                    default=ProductType.PHYSICAL)

    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    compare_at_price = models.DecimalField(max_digits=10, decimal_places=2, null=True,
                                           blank=True)  # struck-through "was" price
    currency = models.CharField(max_length=3, default="NGN")

    # null = unlimited / not tracked (e.g. digital goods)
    stock_quantity = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT,
                              db_index=True)
    is_featured = models.BooleanField(default=False)

    # Card presentation: pill over the image (e.g. "Best seller") and the small
    # text next to the price (e.g. "296 pages"). Both optional, free text.
    badge = models.CharField(max_length=40, blank=True)
    detail_label = models.CharField(max_length=60, blank=True)

    # Downloadable file for digital products. Never exposed by URL; buyers get it
    # through the gated order download endpoint.
    digital_file = models.FileField(upload_to="shop_digital/", blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "category"])]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Product, self.title, self.pk)
        super().save(*args, **kwargs)

    @property
    def in_stock(self) -> bool:
        return self.stock_quantity is None or self.stock_quantity > 0

    def __str__(self):
        return self.title


class ShopOrder(TimeStampedModel):
    """A bag checkout. Separate from payments.Order, which is one-course-per-order."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending payment")
        PAID = "paid", _("Paid")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")

    class Fulfillment(models.TextChoices):
        UNFULFILLED = "unfulfilled", _("Unfulfilled")
        SHIPPED = "shipped", _("Shipped")
        DELIVERED = "delivered", _("Delivered")

    class Provider(models.TextChoices):
        PAYSTACK = "paystack", _("Paystack")
        MANUAL = "manual", _("Manual bank transfer")

    uid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    reference = models.CharField(max_length=100, unique=True, db_index=True)  # "shop-..."
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             related_name="shop_orders")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING,
                              db_index=True)
    fulfillment_status = models.CharField(max_length=20, choices=Fulfillment.choices,
                                          default=Fulfillment.UNFULFILLED, db_index=True)

    total = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="NGN")

    payment_provider = models.CharField(max_length=20, choices=Provider.choices, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)

    # Delivery snapshot (blank for digital-only orders)
    delivery_name = models.CharField(max_length=150, blank=True)
    delivery_phone = models.CharField(max_length=30, blank=True)
    delivery_address = models.CharField(max_length=300, blank=True)
    delivery_city = models.CharField(max_length=100, blank=True)
    delivery_state = models.CharField(max_length=100, blank=True)

    # True while the order's stock is held back from the product (reserved at checkout,
    # returned on cancel/fail/expiry/restocked refund).
    stock_reserved = models.BooleanField(default=False)
    admin_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def requires_delivery(self) -> bool:
        return any(i.product_type == Product.ProductType.PHYSICAL for i in self.items.all())

    def __str__(self):
        return f"{self.reference} ({self.status})"


class ShopOrderItem(TimeStampedModel):
    order = models.ForeignKey(ShopOrder, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="order_items")
    # Snapshots, so history survives product edits/deletion
    title = models.CharField(max_length=200)
    product_type = models.CharField(max_length=20, choices=Product.ProductType.choices)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveSmallIntegerField(default=1)

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class ShopManualPayment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING_REVIEW = "pending_review", _("Pending review")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    order = models.OneToOneField(ShopOrder, on_delete=models.CASCADE,
                                 related_name="manual_payment")
    proof_file = models.FileField(upload_to="shop_payment_proofs/")
    bank_reference = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.PENDING_REVIEW)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=300, blank=True)
