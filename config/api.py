from django.http import Http404
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, ValidationError
from ninja_jwt.controller import NinjaJWTDefaultController
from ninja_extra import NinjaExtraAPI
from apps.accounts.api import router as accounts_router
from apps.catalog.api import router as catalog_router
from apps.payments.api import router as payments_router
from apps.assessments.api import router as assessments_router
from apps.notifications.api import router as notifications_router
from apps.live.api import router as live_router
from apps.shop.api import router as shop_router

api = NinjaExtraAPI(
    title="Kalaro Institute LMS API",
    version="1.0.0",
    description="Backend API for the Kalaro Institute learning platform.",
    docs_url="/docs",
)

api.register_controllers(NinjaJWTDefaultController)


def error(code: str, message: str, details=None, status: int = 400):
    return api.create_response(
        None,
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=status,
    )


@api.exception_handler(ValidationError)
def on_validation_error(request, exc):
    return api.create_response(
        request,
        {"error": {"code": "validation_error", "message": "Invalid input.",
                   "details": exc.errors}},
        status=422,
    )


@api.exception_handler(AuthenticationError)
def on_auth_error(request, exc):
    return api.create_response(
        request,
        {"error": {"code": "unauthenticated", "message": "Authentication required.",
                   "details": {}}},
        status=401,
    )


@api.exception_handler(Http404)
def on_not_found(request, exc):
    return api.create_response(
        request,
        {"error": {"code": "not_found", "message": "Resource not found.", "details": {}}},
        status=404,
    )


@api.get("/health", auth=None, tags=["system"])
def health(request):
    return {"status": "ok"}


api.add_router("/auth", accounts_router)
api.add_router("/catalog", catalog_router)
api.add_router("/payments", payments_router)
api.add_router("/assessments", assessments_router)
api.add_router("/notifications", notifications_router)
api.add_router("/live", live_router)
api.add_router("/shop", shop_router)
