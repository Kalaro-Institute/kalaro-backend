from celery import shared_task

from .services import expire_stale_orders


@shared_task
def expire_stale_shop_orders() -> int:
    """Cancel unpaid shop orders past the hold window and free their reserved stock."""
    return expire_stale_orders()
