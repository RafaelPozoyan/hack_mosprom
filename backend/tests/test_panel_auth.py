from types import SimpleNamespace

import httpx
import pytest

from app import main
from app.api import admin


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["", "panel-test-secret"])
async def test_panel_access_probe_is_protected_and_does_not_expose_password(monkeypatch, password):
    monkeypatch.setattr(admin, "settings", lambda: SimpleNamespace(admin_token=password))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as api:
        for provided in (None, "wrong", password):
            headers = {"Authorization": f"Bearer {provided}"} if provided is not None else {}
            response = await api.get("/api/auth", headers=headers)
            if password and provided != password:
                assert response.status_code == 401
            else:
                assert response.status_code == 200
                assert response.json() == {"required": bool(password)}
        assert (await api.get("/health")).status_code == 200
