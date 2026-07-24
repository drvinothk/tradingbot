"""Identity & access domain: Platform Users are distinct from Broker Accounts
(one user can manage multiple broker accounts later without a redesign) and a
Workspace placeholder exists from day one even though only one workspace row
will ever be created for the foreseeable single-user future.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base, TimestampMixin, UUIDPkMixin


class BrokerType(enum.StrEnum):
    SHOONYA = "shoonya"


class BrokerAccountStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class BrokerAccessLevel(enum.StrEnum):
    OWNER = "owner"
    TRADER = "trader"
    VIEWER = "viewer"


class Workspace(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120), unique=True)

    users: Mapped[list[User]] = relationship(back_populates="workspace")
    broker_accounts: Mapped[list[BrokerAccount]] = relationship(back_populates="workspace")


class User(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "users"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    workspace: Mapped[Workspace] = relationship(back_populates="users")
    role_links: Mapped[list[UserRole]] = relationship(back_populates="user")
    sessions: Mapped[list[LoginSession]] = relationship(back_populates="user")
    broker_access: Mapped[list[UserBrokerAccess]] = relationship(back_populates="user")


class Role(Base, UUIDPkMixin):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(50), unique=True)

    permission_links: Mapped[list[RolePermission]] = relationship(back_populates="role")
    user_links: Mapped[list[UserRole]] = relationship(back_populates="role")


class Permission(Base, UUIDPkMixin):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")

    role_links: Mapped[list[RolePermission]] = relationship(back_populates="permission")


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id"), primary_key=True
    )

    role: Mapped[Role] = relationship(back_populates="permission_links")
    permission: Mapped[Permission] = relationship(back_populates="role_links")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id"), primary_key=True
    )

    user: Mapped[User] = relationship(back_populates="role_links")
    role: Mapped[Role] = relationship(back_populates="user_links")


class LoginSession(Base, UUIDPkMixin):
    """Login/auth session — distinct from `trading_sessions` (the trading-day
    concept), which lives in the strategy/runtime domain."""

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class BrokerAccount(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "broker_accounts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    broker_type: Mapped[BrokerType] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(120))
    credentials_ref: Mapped[str] = mapped_column(
        String(255), doc="Pointer into config/credentials, never the secret itself"
    )
    status: Mapped[BrokerAccountStatus] = mapped_column(
        String(20), default=BrokerAccountStatus.ACTIVE
    )
    primary_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backup_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="broker_accounts")
    user_access: Mapped[list[UserBrokerAccess]] = relationship(back_populates="broker_account")


class UserBrokerAccess(Base):
    __tablename__ = "user_broker_access"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id"), primary_key=True
    )
    access_level: Mapped[BrokerAccessLevel] = mapped_column(String(20))

    user: Mapped[User] = relationship(back_populates="broker_access")
    broker_account: Mapped[BrokerAccount] = relationship(back_populates="user_access")

    __table_args__ = (
        UniqueConstraint("user_id", "broker_account_id", name="uq_user_broker_account"),
    )
