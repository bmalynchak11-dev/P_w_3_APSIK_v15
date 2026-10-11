import pytest
import yaml
from conftest import load

make_idempotent = load(5).make_idempotent


def test_standard_case():
    """Звичайний випадок: базовий набір замін mkdir, touch, echo >> та chmod."""
    src = """- name: Setup test
  hosts: localhost
  tasks:
    - name: Make dir
      command: mkdir -p /var/log/my
    - name: Create file
      command: touch /var/log/my/app.log
    - name: Append line
      shell: echo "started" >> /var/log/my/app.log
    - name: Fix perm
      command: chmod 644 /var/log/my/app.log
"""
    result = make_idempotent(src)
    parsed = yaml.safe_load(result)
    tasks = parsed[0]["tasks"]

    assert "ansible.builtin.file" in tasks[0]
    assert tasks[0]["ansible.builtin.file"]["state"] == "directory"

    assert "ansible.builtin.file" in tasks[1]
    assert tasks[1]["ansible.builtin.file"]["state"] == "touch"

    assert "ansible.builtin.lineinfile" in tasks[2]
    assert tasks[2]["ansible.builtin.lineinfile"]["create"] is True

    assert "ansible.builtin.file" in tasks[3]
    assert tasks[3]["ansible.builtin.file"]["mode"] == "0644"


def test_edge_case_args_creates_preserved():
    """Граничний випадок: завдання з args (creates) або складні команди не змінюються."""
    src = """- name: Preserve test
  hosts: localhost
  tasks:
    - name: Init with creates
      command: touch /var/.initialized
      args:
        creates: /var/.initialized
    - name: Complex pipeline
      shell: ls /var | wc -l
      changed_when: false
"""
    result = make_idempotent(src)
    parsed = yaml.safe_load(result)
    tasks = parsed[0]["tasks"]

    assert "command" in tasks[0] or "ansible.builtin.command" in tasks[0]
    assert tasks[0]["args"]["creates"] == "/var/.initialized"

    assert "shell" in tasks[1] or "ansible.builtin.shell" in tasks[1]
    assert tasks[1]["changed_when"] is False


def test_custom_command_skipped():
    """Випадок з довільною командою: нестандартна команда залишається без заміни."""
    src = """- name: System check
  hosts: localhost
  tasks:
    - name: Run uname
      ansible.builtin.command: uname -a
"""
    result = make_idempotent(src)
    parsed = yaml.safe_load(result)
    tasks = parsed[0]["tasks"]

    assert "ansible.builtin.command" in tasks[0]
    assert tasks[0]["ansible.builtin.command"] == "uname -a"