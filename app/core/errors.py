"""Application errors. Every error carries a stable error code."""


class AppError(Exception):
    status_code = 400
    error_code = "BAD_REQUEST"

    def __init__(self, message=None, *, error_code=None, status_code=None, details=None):
        super().__init__(message or self.__class__.__name__)
        self.message = message or "Request could not be processed"
        if error_code:
            self.error_code = error_code
        if status_code:
            self.status_code = status_code
        self.details = details or {}


class ValidationError(AppError):
    status_code = 422
    error_code = "VALIDATION_ERROR"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "AUTHENTICATION_REQUIRED"


class PermissionDenied(AppError):
    status_code = 403
    error_code = "PERMISSION_DENIED"


class NotFound(AppError):
    status_code = 404
    error_code = "NOT_FOUND"


class Conflict(AppError):
    status_code = 409
    error_code = "CONFLICT"


class InvalidTransition(AppError):
    status_code = 409
    error_code = "INVALID_STATUS_TRANSITION"


class BusinessRuleViolation(AppError):
    status_code = 422
    error_code = "BUSINESS_RULE_VIOLATION"


class RateLimited(AppError):
    status_code = 429
    error_code = "RATE_LIMITED"


class IntegrationError(AppError):
    status_code = 502
    error_code = "INTEGRATION_ERROR"
