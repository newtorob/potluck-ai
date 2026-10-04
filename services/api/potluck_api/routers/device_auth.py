# Real annotations are required by slowapi's FastAPI wrapper.
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from limits import parse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.config import get_settings
from potluck_api.db.database import get_db
from potluck_api.middleware.rate_limit import limiter
from potluck_api.models.member import Member
from potluck_api.services.auth_service import AuthService
from potluck_api.services.device_auth import DeviceAuthError, DeviceAuthService
from potluck_api.services.email_service import EmailService

router = APIRouter()


def reply(body: dict, code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def remember_browser(response: JSONResponse, raw_token: str) -> None:
    """Same HttpOnly cookie /auth/callback sets; Lax keeps it off cross-site requests."""
    settings = get_settings()
    response.set_cookie(key=settings.session_cookie_name, value=raw_token,
                        max_age=settings.session_expiry_days * 24 * 60 * 60, httponly=True,
                        secure=settings.cookie_secure(), samesite=settings.cookie_samesite(), path="/")


async def browser_member(request: Request, db: AsyncSession) -> Member | None:
    """The member behind this browser's session cookie. Bearer tokens never count."""
    raw = request.cookies.get(get_settings().session_cookie_name)
    return await AuthService(db).validate_session(raw) if raw else None


def from_site(request: Request) -> bool:
    # Defense in depth on top of SameSite=Lax and the CORS allowlist.
    return request.headers.get("origin") == get_settings().app_url.rstrip("/")


class CodeEmail(BaseModel):
    user_code: str = Field(min_length=8, max_length=32)
    email: EmailStr


class Proof(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class Decision(Proof):
    user_code: str = Field(min_length=8, max_length=32)
    approved: bool
    remember: bool = False


class SessionDecision(BaseModel):
    user_code: str = Field(min_length=8, max_length=32)
    approved: bool


class Poll(BaseModel):
    device_code: str = Field(min_length=20, max_length=128)


@router.post("/start")
@limiter.limit("10/minute")
@limiter.limit("1000/hour", key_func=lambda: "device-start:global")
async def start(request: Request, db: AsyncSession = Depends(get_db)):
    return reply(await DeviceAuthService(db).start())


@router.post("/request-link")
@limiter.limit("10/minute")
@limiter.limit("500/hour", key_func=lambda: "request-link:global")
async def request_link(request: Request, body: CodeEmail, db: AsyncSession = Depends(get_db)):
    email = str(body.email).strip().lower()
    if limiter.enabled and not limiter.limiter.hit(parse("3/hour"), "email", email):
        raise HTTPException(429, "Too many sign-in requests for this address. Try again later.")
    try:
        link = await DeviceAuthService(db).email_link(body.user_code, email)
    except DeviceAuthError:
        # Avoid enumerating codes/accounts, and never send mail for invalid codes.
        return reply({"status": "sent"}, 202)
    await EmailService().send_device_approval(email, link)
    return reply({"status": "sent"}, 202)


@router.post("/review")
@limiter.limit("30/minute")
async def review(request: Request, body: Proof, db: AsyncSession = Depends(get_db)):
    try:
        proof = DeviceAuthService(db).proof(body.token)
        return reply({"email": proof["email"]})
    except DeviceAuthError as exc:
        return reply({"error": str(exc)}, 400)


@router.post("/approve")
@limiter.limit("10/minute")
async def approve(request: Request, body: Decision, db: AsyncSession = Depends(get_db)):
    try:
        result, browser_token = await DeviceAuthService(db).approve(
            body.token, body.user_code, body.approved, remember=body.remember)
    except DeviceAuthError as exc:
        return reply({"error": str(exc)}, 400)
    response = reply(result)
    if browser_token:
        remember_browser(response, browser_token)
    return response


@router.get("/session")
@limiter.limit("60/minute")
async def session(request: Request, db: AsyncSession = Depends(get_db)):
    member = await browser_member(request, db)
    return reply({"signedIn": True, "email": member.email} if member else {"signedIn": False})


@router.post("/approve-session")
@limiter.limit("10/minute")
async def approve_session(request: Request, body: SessionDecision, db: AsyncSession = Depends(get_db)):
    if not from_site(request):
        return reply({"error": "forbidden_origin"}, 403)
    member = await browser_member(request, db)
    if member is None:
        return reply({"error": "not_signed_in"}, 401)
    try:
        return reply(await DeviceAuthService(db).approve_as(member, body.user_code, body.approved))
    except DeviceAuthError as exc:
        return reply({"error": str(exc)}, 400)


@router.post("/token")
@limiter.limit("60/minute")
async def token(request: Request, body: Poll, db: AsyncSession = Depends(get_db)):
    result = await DeviceAuthService(db).poll(body.device_code)
    return reply(result, 400 if "error" in result else 200)
