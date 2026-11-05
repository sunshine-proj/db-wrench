from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import (
    admin,
    administration,
    auth,
    connections,
    editor,
    explorer,
    governance,
    importing,
    query,
    sso,
    tokens,
    users,
)
from app.auth.deps import origin_guard

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(origin_guard)])
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(connections.router)
api_router.include_router(explorer.router)
api_router.include_router(editor.router)
api_router.include_router(editor.mongo)
api_router.include_router(editor.markers)
api_router.include_router(query.router)
api_router.include_router(query.settings_router)
api_router.include_router(admin.router)
api_router.include_router(administration.router)
api_router.include_router(administration.mongo)
api_router.include_router(governance.router)
api_router.include_router(tokens.router)
api_router.include_router(importing.router)
api_router.include_router(importing.mongo)
api_router.include_router(sso.router)
api_router.include_router(sso.settings_router)
