"""Tests for MonarchClient's credential login when no cached session works."""
import pytest

pytestmark = pytest.mark.unit


class FakeMonarchMoney:
    """Stands in for monarchmoney.MonarchMoney; records login kwargs."""

    login_error: Exception | None = None
    instances: list["FakeMonarchMoney"] = []

    def __init__(self):
        self.login_kwargs = None
        self.saved_to = None
        FakeMonarchMoney.instances.append(self)

    async def login(self, **kwargs):
        self.login_kwargs = kwargs
        if FakeMonarchMoney.login_error is not None:
            raise FakeMonarchMoney.login_error

    def save_session(self, path):
        self.saved_to = path


@pytest.fixture
def client(tmp_path, monkeypatch):
    import monarchmoney
    from api.services import monarch as mod

    FakeMonarchMoney.login_error = None
    FakeMonarchMoney.instances = []
    monkeypatch.setattr(monarchmoney, "MonarchMoney", FakeMonarchMoney)
    monkeypatch.setattr(mod, "SESSION_PATH", tmp_path / "monarch_session.pickle")
    monkeypatch.setattr(mod.settings, "monarch_email", "synthetic@example.com")
    monkeypatch.setattr(mod.settings, "monarch_password", "synthetic-password")
    return mod.MonarchClient()


async def test_login_passes_mfa_secret_key_when_set(client, monkeypatch):
    from api.services import monarch as mod
    monkeypatch.setattr(mod.settings, "monarch_mfa_secret_key", "JBSWY3DPEHPK3PXP")

    mm = await client._get_client()

    assert mm.login_kwargs["mfa_secret_key"] == "JBSWY3DPEHPK3PXP"
    assert mm.login_kwargs["use_saved_session"] is False
    assert mm.saved_to == str(mod.SESSION_PATH)


async def test_login_passes_none_when_mfa_secret_key_empty(client, monkeypatch):
    from api.services import monarch as mod
    monkeypatch.setattr(mod.settings, "monarch_mfa_secret_key", "")

    mm = await client._get_client()

    assert mm.login_kwargs["mfa_secret_key"] is None


async def test_mfa_required_raises_actionable_error(client, monkeypatch):
    from monarchmoney import RequireMFAException
    from api.services import monarch as mod
    monkeypatch.setattr(mod.settings, "monarch_mfa_secret_key", "")
    FakeMonarchMoney.login_error = RequireMFAException("Multi-Factor Auth Required")

    with pytest.raises(RuntimeError) as exc_info:
        await client._get_client()

    message = str(exc_info.value)
    assert "scripts/monarch_reauth.py" in message
    assert "MONARCH_MFA_SECRET_KEY" in message
    assert client._mm is None
