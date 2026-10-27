"""Everything the owner does in the private chat. Each module registers its handlers on one router."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from diary.db import Db

from ..common import Alerts, Identity
from . import data, goals, pairing, plan, progress, settings, text
from .ctx import HELP, Ctx
from .pairing import build_claim_router
from .plan import parse_until

__all__ = ["HELP", "build_claim_router", "build_fallback_router", "build_owner_router", "parse_until"]


def build_owner_router(db: Db, ident: Identity, alerts: Alerts | None = None) -> Router:
    router = Router(name="owner")
    ctx = Ctx(db, ident, alerts or Alerts())

    def is_owner(event) -> bool:
        user = event.from_user
        return user is not None and ident.owner_id is not None and user.id == ident.owner_id

    router.message.filter(F.chat.type == "private", is_owner)
    router.callback_query.filter(is_owner)
    # order matters: the catch-all text handlers must come last
    for module in (plan, progress, goals, settings, pairing, data, text):
        module.register(router, ctx)
    return router


def build_fallback_router() -> Router:
    """Last in line: no button press is left hanging, whoever pressed it."""
    router = Router(name="fallback")

    @router.callback_query()
    async def any_callback(cb: CallbackQuery) -> None:
        await cb.answer()

    return router
