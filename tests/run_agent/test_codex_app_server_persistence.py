from types import SimpleNamespace

from run_agent import AIAgent


class FakeCodexSession:
    def __init__(self, turn):
        self.turn = turn

    def run_turn(self, user_input):
        self.user_input = user_input
        return self.turn


def test_codex_app_server_turn_persists_projected_messages():
    agent = AIAgent.__new__(AIAgent)
    agent._codex_session = FakeCodexSession(
        SimpleNamespace(
            final_text="done",
            projected_messages=[
                {"role": "user", "content": "pwd"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "codex_exec_1",
                            "type": "function",
                            "function": {
                                "name": "exec_command",
                                "arguments": '{"command":"pwd"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "codex_exec_1", "content": "/tmp"},
                {"role": "assistant", "content": "done"},
            ],
            tool_iterations=1,
            interrupted=False,
            error=None,
            should_retire=False,
            thread_id="thread-1",
            turn_id="turn-1",
        )
    )
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = set()
    agent._memory_manager = None

    saved_trajectories = []
    cleaned_tasks = []
    persisted = []
    agent._save_trajectory = lambda messages, user_message, completed: saved_trajectories.append(
        (list(messages), user_message, completed)
    )
    agent._cleanup_task_resources = lambda task_id: cleaned_tasks.append(task_id)
    agent._persist_session = lambda messages, conversation_history=None: persisted.append(
        (list(messages), list(conversation_history or []))
    )

    history = [{"role": "user", "content": "previous"}]
    messages = list(history) + [{"role": "user", "content": "pwd"}]

    result = AIAgent._run_codex_app_server_turn(
        agent,
        user_message="pwd",
        original_user_message="pwd",
        messages=messages,
        conversation_history=history,
        effective_task_id="session-1",
    )

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert result["messages"] == messages
    assert [m["role"] for m in messages] == [
        "user",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert saved_trajectories[-1][2] is True
    assert cleaned_tasks == ["session-1"]
    assert persisted[-1][0] == messages
    assert persisted[-1][1] == history
