import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import (clear_session, client_ip, current_user, hash_password, issue_session,
                               login_ip_limiter, login_limiter, verify_password)
from app.models import AuditLog, Membership, User, Workspace

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def _norm_email(v: str) -> str:
    v = (v or "").strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("invalid email")
    return v


class RegisterIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=72)
    name: str = Field(default="", max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _norm_email(v)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return (v or "").strip().lower()


def user_payload(db: Session, user: User) -> dict:
    rows = db.execute(select(Workspace, Membership.role).join(Membership, Membership.workspace_id == Workspace.id)
                      .where(Membership.user_id == user.id).order_by(Workspace.id)).all()
    return {"user": {"id": user.id, "email": user.email, "name": user.name},
            "workspaces": [{"id": w.id, "name": w.name, "role": role} for w, role in rows]}


@router.post("/register")
def register(body: RegisterIn, response: Response, db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise HTTPException(status_code=409, detail="Could not register with these details")
    user = User(email=body.email, name=body.name.strip(), password_hash=hash_password(body.password))
    ws = Workspace(name=body.workspace_name.strip())
    db.add_all([user, ws])
    db.flush()
    db.add(Membership(user_id=user.id, workspace_id=ws.id, role="owner"))
    db.add(AuditLog(workspace_id=ws.id, user_id=user.id, action="register", detail={}))
    db.commit()
    issue_session(response, user.id)
    return user_payload(db, user)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    key = f"{ip}|{body.email}"
    if login_limiter.blocked(key) or login_ip_limiter.blocked(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    user = db.scalar(select(User).where(User.email == body.email))
    if not verify_password(body.password, user.password_hash if user else None) or not user or not user.is_active:
        login_limiter.hit(key)
        login_ip_limiter.hit(ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    login_limiter.reset(key)
    issue_session(response, user.id)
    return user_payload(db, user)


@router.post("/logout")
def logout(response: Response):
    clear_session(response)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return user_payload(db, user)
