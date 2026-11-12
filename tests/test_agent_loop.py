"""Runs the Gemini agent loop against a scripted fake client (no network)."""

import httpx2
import pytest
from google.genai import errors, types

from opspilot import agent as agent_mod
from opspilot.agent import GeminiAgent
from opspilot.audit import AuditLog
from opspilot.orchestrator import Orchestrator
from opspilot.policy import Requester


@pytest.fixture(autouse=True)
def _clear_model_cooldowns():
    agent_mod.MODEL_COOLDOWN.clear()
    yield
    agent_mod.MODEL_COOLDOWN.clear()


def call(name, args, cid):
    return types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args))


def reply(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)), finish_reason=types.FinishReason.STOP)])


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append(list(contents))
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def test_loop_routes_calls_through_policy(tmp_path):
    client = FakeClient([
        reply(call("lookup_user", {"user_id": "alice"}, "c1"),
              call("reset_password", {"user_id": "bob"}, "c2")),  # parallel calls; second is denied
        reply(call("unlock_account", {"user_id": "alice"}, "c3")),
        reply(types.Part(text="Your account is unlocked.")),
    ])
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    agent = GeminiAgent(orch, model="test-model", client=client)
    seen = []
    out = agent.ask("I'm locked out", on_tool=lambda n, a, r: seen.append((n, r)))

    assert out == "Your account is unlocked."
    assert [n for n, _ in seen] == ["lookup_user", "reset_password", "unlock_account"]
    assert seen[1][1]["error"] == "denied_by_policy"
    assert orch.systems.directory.users["alice"]["locked"] is False

    # Function responses are sent back with the matching call ids.
    second_request = client.models.requests[1]
    fr = [p.function_response for p in second_request[-1].parts]
    assert [f.id for f in fr] == ["c1", "c2"]


def test_step_limit_hands_off(tmp_path):
    loop = [reply(call("list_active_alerts", {"scope": "all"}, f"c{i}")) for i in range(3)]
    orch = Orchestrator(Requester("dana", "it_admin"), AuditLog(tmp_path / "a.jsonl"))
    agent = GeminiAgent(orch, model="m", client=FakeClient(loop), max_steps=3)
    assert "human" in agent.ask("loop forever")


def test_tool_declarations_cover_registry(tmp_path):
    orch = Orchestrator(Requester("dana", "it_admin"), AuditLog(tmp_path / "a.jsonl"))
    agent = GeminiAgent(orch, model="m", client=FakeClient([]))
    names = {d.name for d in agent.config.tools[0].function_declarations}
    assert names == {n for n, t in orch.tools.items() if t.model_visible}
    assert "review_kb_article" not in names  # humans review the model's KB drafts


class FlakyModels(FakeModels):
    def generate_content(self, model, contents, config):
        if model == "busy-model":
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
        return super().generate_content(model, contents, config)


def test_failover_to_backup_model_on_503(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "backup-model")
    client = FakeClient([reply(types.Part(text="hi"))])
    client.models = FlakyModels(client.models.script)
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    agent = GeminiAgent(orch, model="busy-model", client=client)
    assert agent.ask("hello") == "hi"
    assert agent.model == "backup-model"
    assert "model_failover" in (tmp_path / "a.jsonl").read_text()


def test_all_models_down_returns_friendly_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    client = FakeClient([])
    client.models = FlakyModels([])
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    out = GeminiAgent(orch, model="busy-model", client=client, backoff_s=0).ask("hello")
    assert "unavailable" in out


class TimeoutModels(FakeModels):
    def generate_content(self, model, contents, config):
        if model == "slow-model":
            raise httpx2.ReadTimeout("timed out")
        return super().generate_content(model, contents, config)


def test_timeout_fails_over_to_next_model(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "backup-model")
    client = FakeClient([])
    client.models = TimeoutModels([reply(types.Part(text="hi"))])
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    agent = GeminiAgent(orch, model="slow-model", client=client)
    assert agent.ask("hello") == "hi"
    assert agent.model == "backup-model"
    assert "ReadTimeout" in (tmp_path / "a.jsonl").read_text()


def thinking_400(level):
    return errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
        "message": f"Thinking level {level} is not supported for this model. Please retry with other thinking level."}})


class PickyThinkingModels(FakeModels):
    """Like gemini-3.7/3.8-flash: rejects MINIMAL thinking, accepts LOW."""
    def __init__(self, script):
        super().__init__(script)
        self.levels = []

    def generate_content(self, model, contents, config):
        level = config.thinking_config.thinking_level if config.thinking_config else None
        self.levels.append((model, str(level)))
        if level is not None and "MINIMAL" in str(level).upper():
            raise thinking_400("MINIMAL")
        return super().generate_content(model, contents, config)


def test_unsupported_thinking_level_steps_up_on_same_model(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "minimal")
    client = FakeClient([])
    client.models = PickyThinkingModels([reply(types.Part(text="one")), reply(types.Part(text="two"))])
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    agent = GeminiAgent(orch, model="picky-model", client=client)
    assert agent.ask("hello") == "one"
    assert agent.ask("again") == "two"
    # First call retried the same model at LOW; the second call remembered LOW.
    assert [m for m, _ in client.models.levels] == ["picky-model"] * 3
    assert ["MINIMAL" in lvl.upper() for _, lvl in client.models.levels] == [True, False, False]
    log = tmp_path / "a.jsonl"
    assert not log.exists() or "model_failover" not in log.read_text()


class BadRequestModels(FakeModels):
    def generate_content(self, model, contents, config):
        raise errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})


def test_other_400_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "backup-model")
    client = FakeClient([])
    client.models = BadRequestModels([])
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    out = GeminiAgent(orch, model="m", client=client).ask("hello")
    assert "unavailable" in out and "400" in out


def quota_429(retry="66472s"):
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry}]}})


class QuotaModels(FakeModels):
    def __init__(self, script):
        super().__init__(script)
        self.tried = []

    def generate_content(self, model, contents, config):
        self.tried.append(model)
        if model == "out-of-quota":
            raise quota_429()
        return super().generate_content(model, contents, config)


def test_out_of_quota_model_is_skipped_on_later_steps_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "backup-model")
    script = [reply(call("lookup_user", {"user_id": "alice"}, "c1")),
              reply(types.Part(text="done")), reply(types.Part(text="hi again"))]
    client = FakeClient([])
    client.models = QuotaModels(script)
    orch = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "a.jsonl"))
    assert GeminiAgent(orch, model="out-of-quota", client=client).ask("help") == "done"
    # A brand-new session that prefers the exhausted model goes straight to the backup.
    orch2 = Orchestrator(Requester("alice", "employee"), AuditLog(tmp_path / "b.jsonl"))
    assert GeminiAgent(orch2, model="out-of-quota", client=client).ask("hello") == "hi again"
    assert client.models.tried == ["out-of-quota", "backup-model", "backup-model", "backup-model"]


def test_429_cooldown_uses_retry_delay_capped():
    assert agent_mod._cooldown_s(quota_429("42s")) == 42
    assert agent_mod._cooldown_s(quota_429("66472s")) == agent_mod.MAX_COOLDOWN_S
    assert agent_mod._cooldown_s(httpx2.ReadTimeout("t")) == 300
