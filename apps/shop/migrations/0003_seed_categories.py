from django.db import migrations

DEFAULT_CATEGORIES = ["Books", "Templates", "Kits", "Apparel"]


def seed(apps, schema_editor):
    ProductCategory = apps.get_model("shop", "ProductCategory")
    for order, name in enumerate(DEFAULT_CATEGORIES):
        ProductCategory.objects.get_or_create(
            name=name, defaults={"slug": name.lower(), "order": order}
        )


class Migration(migrations.Migration):
    dependencies = [("shop", "0002_product_badge_detail_label")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
