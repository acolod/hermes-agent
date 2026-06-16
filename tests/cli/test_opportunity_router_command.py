from types import SimpleNamespace
from unittest.mock import patch

from cli import HermesCLI


def _make_cli() -> HermesCLI:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._pending_resume_sessions = []
    cli_obj._command_running = False
    cli_obj._command_status = ""
    cli_obj._last_invalidate = 0.0
    cli_obj._app = None
    cli_obj.profile = "default"
    cli_obj.session_id = "cli-test-session"
    return cli_obj


def test_process_command_dispatches_opportunity_router():
    cli_obj = _make_cli()
    with patch.object(cli_obj, "_handle_opportunity_router_command") as handler:
        assert cli_obj.process_command("/opportunity-router Vet this idea") is True
    handler.assert_called_once_with("/opportunity-router Vet this idea")


def test_handle_opportunity_router_routes_and_prints_result(capsys):
    cli_obj = _make_cli()
    outcome = SimpleNamespace(final_response="Verdict: pursue")

    with patch("agent.opportunity_routing.route_opportunity_request", return_value=outcome) as route_mock:
        cli_obj._handle_opportunity_router_command(
            "/opportunity-router Vet this idea: B2B workflow audits for local venues."
        )

    output = capsys.readouterr().out
    assert "Routing to opportunity-radar specialist..." in output
    assert "Verdict: pursue" in output
    assert route_mock.call_count == 1
    kwargs = route_mock.call_args.kwargs
    assert kwargs["user_message"] == "Vet this idea: B2B workflow audits for local venues."
    assert kwargs["original_user_message"] == kwargs["user_message"]
    assert kwargs["source_platform"] == "cli"


def test_handle_opportunity_router_without_prompt_shows_usage(capsys):
    cli_obj = _make_cli()

    cli_obj._handle_opportunity_router_command("/opportunity-router")

    output = capsys.readouterr().out
    assert "Usage: /opportunity-router <prompt>" in output
