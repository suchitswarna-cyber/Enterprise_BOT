"""Runs the Gemini agent loop against a scripted fake client (no network)."""

from google.genai import types

from opspilot.agent import GeminiAgent
from opspilot.audit import AuditLog
from opspilot.orchestrator import Orchestrator
from opspilot.policy import Requester


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
    assert names == set(orch.tools)
