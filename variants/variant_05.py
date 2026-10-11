import copy
import re
import shlex
import yaml


def _clean_quotes(val: str) -> str:
    """Видаляє зовнішні парні лапки, якщо вони залишилися після shlex."""
    if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
        return val[1:-1]
    return val


def _transform_task(task_dict: dict) -> dict:
    new_task = copy.deepcopy(task_dict)
    
    # Визначаємо ключ модуля
    module_key = None
    target_keys = ["command", "shell", "ansible.builtin.command", "ansible.builtin.shell"]
    for k in target_keys:
        if k in new_task:
            module_key = k
            break
            
    if not module_key:
        return new_task

    # Якщо присутній ключ args (наприклад, creates), згідно з правилами завдання залишається без змін
    if "args" in new_task:
        return new_task

    cmd_val = new_task[module_key]
    if not isinstance(cmd_val, str):
        return new_task

    raw_cmd = cmd_val.strip()

    # Спроба розбити команду через shlex
    try:
        tokens = shlex.split(raw_cmd)
    except Exception:
        return new_task

    if not tokens:
        return new_task

    cmd_name = tokens[0]
    matched = False
    new_module = None
    new_args = None

    # 1. mkdir [-p] ШЛЯХ -> ansible.builtin.file (state: directory)
    if cmd_name == "mkdir":
        args_rest = tokens[1:]
        path = None
        if len(args_rest) == 1 and not args_rest[0].startswith("-"):
            path = args_rest[0]
        elif len(args_rest) == 2 and args_rest[0] == "-p":
            path = args_rest[1]
        
        if path is not None:
            matched = True
            new_module = "ansible.builtin.file"
            new_args = {
                "path": _clean_quotes(path),
                "state": "directory"
            }

    # 2. touch ШЛЯХ -> ansible.builtin.file (state: touch, preserve times)
    elif cmd_name == "touch":
        args_rest = tokens[1:]
        if len(args_rest) == 1:
            matched = True
            new_module = "ansible.builtin.file"
            new_args = {
                "path": _clean_quotes(args_rest[0]),
                "state": "touch",
                "modification_time": "preserve",
                "access_time": "preserve"
            }

    # 3. chmod РЕЖИМ ШЛЯХ (режим з 3 або 4 цифр) -> ansible.builtin.file (mode з 4 цифр, zfill)
    elif cmd_name == "chmod":
        args_rest = tokens[1:]
        if len(args_rest) == 2 and re.match(r"^\d{3,4}$", args_rest[0]):
            mode_str = args_rest[0].zfill(4)
            matched = True
            new_module = "ansible.builtin.file"
            new_args = {
                "path": _clean_quotes(args_rest[1]),
                "mode": mode_str
            }

    # 4. ln -s ДЖЕРЕЛО ПОСИЛАННЯ -> ansible.builtin.file (state: link)
    elif cmd_name == "ln":
        args_rest = tokens[1:]
        if len(args_rest) == 3 and args_rest[0] == "-s":
            matched = True
            new_module = "ansible.builtin.file"
            new_args = {
                "src": _clean_quotes(args_rest[1]),
                "dest": _clean_quotes(args_rest[2]),
                "state": "link"
            }

    # 5. rm [-f] ШЛЯХ -> ansible.builtin.file (state: absent)
    elif cmd_name == "rm":
        args_rest = tokens[1:]
        path = None
        if len(args_rest) == 1 and not args_rest[0].startswith("-"):
            path = args_rest[0]
        elif len(args_rest) == 2 and args_rest[0] == "-f":
            path = args_rest[1]

        if path is not None:
            matched = True
            new_module = "ansible.builtin.file"
            new_args = {
                "path": _clean_quotes(path),
                "state": "absent"
            }

    # 6. cp ДЖЕРЕЛО ПРИЗНАЧЕННЯ -> ansible.builtin.copy (remote_src: true)
    elif cmd_name == "cp":
        args_rest = tokens[1:]
        if len(args_rest) == 2:
            matched = True
            new_module = "ansible.builtin.copy"
            new_args = {
                "src": _clean_quotes(args_rest[0]),
                "dest": _clean_quotes(args_rest[1]),
                "remote_src": True
            }

    # 7. echo TEKCT... >> ШЛЯХ -> ansible.builtin.lineinfile (create: true)
    elif cmd_name == "echo" and ">>" in raw_cmd:
        parts = raw_cmd.split(">>", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        try:
            echo_tokens = shlex.split(left)
            dest_tokens = shlex.split(right)
            if len(echo_tokens) >= 1 and echo_tokens[0] == "echo" and len(dest_tokens) == 1:
                line_content = " ".join(echo_tokens[1:])
                matched = True
                new_module = "ansible.builtin.lineinfile"
                new_args = {
                    "path": _clean_quotes(dest_tokens[0]),
                    "line": _clean_quotes(line_content),
                    "create": True
                }
        except Exception:
            pass

    if matched:
        # Зберігаємо точний порядок ключів словника завдання
        transformed = {}
        for k, v in new_task.items():
            if k == module_key:
                transformed[new_module] = new_args
            else:
                transformed[k] = v
        return transformed

    return new_task


def make_idempotent(playbook: str) -> str:
    """Замінює типові неідемпотентні команди command/shell на модулі стану."""
    data = yaml.safe_load(playbook)
    if not isinstance(data, list):
        return playbook

    for play in data:
        if isinstance(play, dict) and "tasks" in play and isinstance(play["tasks"], list):
            play["tasks"] = [_transform_task(t) if isinstance(t, dict) else t for t in play["tasks"]]

    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)