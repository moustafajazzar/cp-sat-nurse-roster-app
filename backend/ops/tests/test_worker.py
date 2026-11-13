from django.core.management import get_commands


def test_procrastinate_worker_command_is_installed():
    assert "procrastinate" in get_commands()
