from django.contrib import admin

from .models import Product, ProductCategory

admin.site.register(ProductCategory)
admin.site.register(Product)

from .models import ShopManualPayment, ShopOrder, ShopOrderItem  # noqa: E402

admin.site.register(ShopOrder)
admin.site.register(ShopOrderItem)
admin.site.register(ShopManualPayment)
