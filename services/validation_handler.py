"""
Turns FastAPI's raw 422 validation errors into a single readable message.

Before: {"detail": [{"type": "value_error", "loc": ["body", "email"], ...}]}
After:  {"detail": "Please enter a valid email address."}

Same status code (422) and same "detail" key, so your compiled frontend keeps
working; "detail" is just a string now instead of a list.

Save as services/validation_handler.py and register it once in main.py.
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

FIELD_LABELS = {
    "email": "email address",
    "username": "username",
    "password": "password",
    "first_name": "first name",
    "last_name": "last name",
    "gender": "gender",
    "role": "role",
    "university": "university",
    "organisation": "organisation",
}


def _friendly(err: dict) -> str:
    loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
    field = loc[-1] if loc else "input"
    label = FIELD_LABELS.get(field, field.replace("_", " "))
    etype = err.get("type", "")
    msg = err.get("msg", "")
    ctx = err.get("ctx") or {}

    if field == "email" and ("value_error" in etype or "email" in msg.lower()):
        return "Please enter a valid email address."
    if etype == "missing":
        return f"Please enter your {label}."
    if etype == "string_too_short" and "min_length" in ctx:
        return f"Your {label} must be at least {ctx['min_length']} characters."
    if etype == "string_too_long" and "max_length" in ctx:
        return f"Your {label} must be at most {ctx['max_length']} characters."
    if etype.endswith("_type") or etype.endswith("_parsing"):
        return f"Please enter a valid {label}."

    # Fallback: still readable, never a raw dict
    msg = msg.removeprefix("Value error, ")
    return f"Invalid {label}: {msg}" if msg else f"Invalid {label}."


def register_validation_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        message = _friendly(errors[0]) if errors else "Invalid request."
        # Deliberately do NOT include exc.errors() or exc.body in the response:
        # they echo back what the user submitted, including passwords.
        return JSONResponse(status_code=422, content={"detail": message})