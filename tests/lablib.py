"""Спільна бібліотека перевірок практичної роботи APSIK-P03.

Містить відкриті набори даних (S1-S3 з карток), перевірки властивостей результату і пісочницю для
реального запуску Ansible на localhost у тимчасовому каталозі. Права адміністратора не потрібні:
жодна перевірка не використовує become і не змінює систему.
Якщо ansible-core недоступний, перевірки з реальним запуском пропускаються з причиною.
"""
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

FUNC = {1: "make_inventory", 2: "adhoc_command", 3: "webserver_play", 4: "postgres_play", 5: "make_idempotent",
        6: "analyze_runs", 7: "make_template", 8: "diff_summary", 9: "vault_files", 10: "safe_config_play"}
CHECKS = {}   # номер задачі -> список (ім'я, функція, потрібен_ansible)
DATA = {n: [] for n in range(1, 11)}   # відкриті набори: {"tag", "set", "args", "error"}


# ---------------------------------------------------------------------------------------------
# Пошук Ansible і пісочниця
# ---------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def ansible_bin():
    """Тека з ansible-playbook: змінна APSIK_ANSIBLE_BIN, PATH або тека поточного інтерпретатора."""
    cands = [os.environ.get("APSIK_ANSIBLE_BIN"), str(Path(sys.executable).parent)]
    which = shutil.which("ansible-playbook")
    if which:
        cands.insert(1, str(Path(which).parent))
    for c in cands:
        if c and (Path(c) / "ansible-playbook").exists():
            return c
    return None


def need_ansible():
    if not ansible_bin():
        pytest.skip("ansible-core недоступний: перевірку реальним запуском пропущено (pip install ansible-core)")


CFG = """[defaults]
inventory = inventory.ini
forks = 1
host_key_checking = False
retry_files_enabled = False
nocolor = 1
interpreter_python = auto_silent
"""
INVENTORY = """[web]
web1
web2

[db]
db1

[app]
node1

[prod]
web1
db1

[all:vars]
ansible_connection=local
ansible_python_interpreter={{ ansible_playbook_python }}
root={{ playbook_dir }}/nodes/{{ inventory_hostname }}
"""


class Sandbox:
    """Тимчасовий каталог із ansible.cfg та інвентарем, де «сервери» web1, web2, db1, node1 це localhost."""

    def __init__(self, path):
        self.dir = Path(path).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update(PATH=ansible_bin() + os.pathsep + os.environ["PATH"],
                        ANSIBLE_CONFIG=str(self.dir / "ansible.cfg"), ANSIBLE_LOCALHOST_WARNING="False",
                        ANSIBLE_INVENTORY_UNPARSED_WARNING="False", ANSIBLE_NOCOLOR="1",
                        PYTHONDONTWRITEBYTECODE="1", LC_ALL="C.UTF-8", HOME=str(self.dir))
        for k in ("ANSIBLE_STDOUT_CALLBACK", "ANSIBLE_VAULT_PASSWORD_FILE"):
            self.env.pop(k, None)
        self.write("ansible.cfg", CFG)
        self.write("inventory.ini", INVENTORY)

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel):
        return (self.dir / rel).read_text(encoding="utf-8")

    def run(self, argv, shell=False):
        """Запускає команду; повертає (код, вивід). stdin закрито, бо Ansible вимагає блокуючого вводу-виводу."""
        r = subprocess.run(argv, cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, shell=shell, executable="/bin/bash" if shell else None,
                           timeout=120)
        return r.returncode, r.stdout

    def play(self, rel, *extra):
        return self.run(["ansible-playbook", rel, *extra])


def recap(out):
    """Розбирає PLAY RECAP: {хост: {ok, changed, unreachable, failed, skipped}}."""
    pos = out.rfind("PLAY RECAP")
    rows = {}
    for line in out[pos:].splitlines()[1:] if pos >= 0 else []:
        m = re.match(r"^(\S+)\s*:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+skipped=(\d+)", line)
        if m:
            rows[m.group(1)] = dict(zip(("ok", "changed", "unreachable", "failed", "skipped"),
                                        (int(x) for x in m.groups()[1:])))
    return rows


def total_changed(out):
    return sum(r["changed"] for r in recap(out).values())


# ---------------------------------------------------------------------------------------------
# Допоміжне для YAML
# ---------------------------------------------------------------------------------------------
KEYWORDS = set("""name when register notify become become_user changed_when failed_when no_log tags loop with_items
vars args check_mode ignore_errors delegate_to run_once environment block rescue always listen until retries delay diff
throttle loop_control async poll connection""".split())


def load_plays(text):
    assert isinstance(text, str) and text.strip(), "результат має бути непорожнім рядком"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise AssertionError(f"результат не є коректним YAML: {e}")
    assert isinstance(data, list) and data and all(isinstance(p, dict) for p in data), \
        "плейбук має бути списком ігор (кожна гра це словник)"
    return data


def module_of(task):
    keys = [k for k in task if k not in KEYWORDS]
    assert len(keys) == 1, f"у завданні має бути один модуль, знайдено {keys}"
    return keys[0]


def mod_params(task):
    return task[module_of(task)]


def find_tasks(tasks, module):
    full = "ansible.builtin." + module
    return [t for t in tasks if module_of(t) == full]


def only_play(text):
    plays = load_plays(text)
    assert len(plays) == 1, f"очікується одна гра, знайдено {len(plays)}"
    return plays[0]


def all_tasks(play):
    return list(play.get("pre_tasks") or []) + list(play.get("tasks") or []) + list(play.get("post_tasks") or [])


def truthy(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes"))


def squash(text):
    """Рядки без порожніх, зайві пробіли стиснено."""
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def syntax_check(sbx, text, name="check.yml"):
    sbx.write(name, text)
    rc, out = sbx.play(name, "--syntax-check")
    assert rc == 0, "ansible-playbook --syntax-check відхилив плейбук:\n" + out[-600:]


# ---------------------------------------------------------------------------------------------
# Задача 1. Інвентар
# ---------------------------------------------------------------------------------------------
def _srv(name, address, user, groups, port=None, **extra):
    d = {"name": name, "address": address, "user": user, "groups": groups}
    if port:
        d["port"] = port
    if extra:
        d["vars"] = extra
    return d


INV_S1 = ([_srv("hp-srv", "10.20.0.11", "admin", ["hypervisors"]),
           _srv("dell-1", "10.20.0.12", "admin", ["hypervisors"]),
           _srv("web-01", "10.20.1.21", "student", ["web", "vm"], http_port=8080),
           _srv("web-02", "10.20.1.22", "student", ["web", "vm"]),
           _srv("db-01", "10.20.1.31", "student", ["db", "vm"], pg_port=5432)],
          {"college": ["hypervisors", "vm"], "prod": ["web", "db"]},
          {"all": {"ansible_python_interpreter": "/usr/bin/python3"}, "db": {"pg_version": 16}})
INV_S2 = ([_srv("c-web1", "127.0.0.1", "root", ["web"], 2201),
           _srv("c-web2", "127.0.0.1", "root", ["web"], 2202),
           _srv("c-db", "127.0.0.1", "root", ["db"], 2203, pg_port=5432),
           _srv("c-spare", "127.0.0.1", "root", [], 2204)],
          {"lab": ["web", "db"]},
          {"web": {"http_port": 8080}})
INV_S3 = ([_srv("app-s1", "10.30.0.11", "deploy", ["staging", "app"]),
           _srv("app-p1", "10.30.1.11", "deploy", ["production", "app"], 2222),
           _srv("app-p2", "10.30.1.12", "deploy", ["production", "app"], 2222),
           _srv("db-s1", "10.30.0.21", "deploy", ["staging", "db"], pg_port=5433),
           _srv("db-p1", "10.30.1.21", "deploy", ["production", "db"], 2222, pg_port=5432)],
          {"environments": ["production", "staging"]},
          {"production": {"deploy_env": "prod"}, "staging": {"deploy_env": "stage"},
           "all": {"ansible_python_interpreter": "auto_silent"}})


def parse_ini(text):
    """Мінімальний розбір INI-інвентаря: (вільні хости, групи, діти, змінні груп, порядок розділів)."""
    loose, groups, kids, gvars, order, hostvars = [], {}, {}, {}, [], {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            name = m.group(1)
            kind = "hosts"
            if name.endswith(":children"):
                name, kind = name[:-9], "children"
            elif name.endswith(":vars"):
                name, kind = name[:-5], "vars"
            cur = (name, kind)
            order.append(cur)
            continue
        if cur is None:
            tok = shlex.split(line)
            loose.append(tok[0])
            hostvars.setdefault(tok[0], {}).update(dict(t.split("=", 1) for t in tok[1:]))
        elif cur[1] == "hosts":
            tok = shlex.split(line)
            groups.setdefault(cur[0], []).append(tok[0])
            hostvars.setdefault(tok[0], {}).update(dict(t.split("=", 1) for t in tok[1:]))
            hostvars[tok[0]]["_line_" + cur[0]] = tok
        elif cur[1] == "children":
            kids.setdefault(cur[0], []).append(line)
        else:
            k, v = line.split("=", 1)
            gvars.setdefault(cur[0], {})[k.strip()] = v.strip()
    return loose, groups, kids, gvars, order, hostvars


def inv_expect(args):
    servers, children, gvars = args
    groups = {}
    for s in servers:
        for g in s.get("groups") or []:
            if s["name"] not in groups.setdefault(g, []):
                groups[g].append(s["name"])
    closure = {}

    def members(g, seen=()):
        out = list(groups.get(g, []))
        for c in (children or {}).get(g, []):
            for h in members(c):
                if h not in out:
                    out.append(h)
        return out
    for g in set(groups) | set(children or {}):
        closure[g] = set(members(g))
    hv = {}
    for s in servers:
        d = {"ansible_host": s["address"], "ansible_user": s["user"]}
        if "port" in s:
            d["ansible_port"] = str(s["port"])
        d.update({k: str(v) for k, v in (s.get("vars") or {}).items()})
        hv[s["name"]] = d
    return groups, closure, hv


def c1_model(ds, text):
    assert isinstance(text, str) and text.strip()
    loose, groups, kids, gvars, order, hostvars = parse_ini(text)
    exp_groups, _, _ = inv_expect(ds["args"])
    servers, children, gv = ds["args"]
    assert groups == {g: list(h) for g, h in exp_groups.items()}, f"хости в групах: {groups}, очікується {exp_groups}"
    assert loose == [s["name"] for s in servers if not s.get("groups")], f"хости без груп: {loose}"
    assert kids == {p: list(c) for p, c in (children or {}).items() if c}, f"дочірні групи: {kids}"
    assert gvars == {g: {k: str(v) for k, v in d.items()} for g, d in (gv or {}).items() if d}, f"змінні груп: {gvars}"


def c1_hostvars(ds, text):
    _, groups, _, _, _, hostvars = parse_ini(text)
    _, _, hv = inv_expect(ds["args"])
    for name, want in hv.items():
        got = {k: v for k, v in hostvars[name].items() if not k.startswith("_line_")}
        assert got == want, f"змінні вузла {name}: {got}, очікується {want}"
        lines = [v for k, v in hostvars[name].items() if k.startswith("_line_")]
        for tok in lines:   # однакові на всіх рядках вузла
            assert dict(t.split("=", 1) for t in tok[1:]) == want


def c1_layout(ds, text):
    loose, groups, kids, gvars, order, hostvars = parse_ini(text)
    host_secs = [n for n, k in order if k == "hosts"]
    assert host_secs == sorted(host_secs), f"групи хостів не за абеткою: {host_secs}"
    assert [n for n, k in order if k == "children"] == sorted(n for n, k in order if k == "children")
    assert [n for n, k in order if k == "vars"] == sorted(n for n, k in order if k == "vars")
    kinds = [k for _, k in order]
    assert kinds == sorted(kinds, key=["hosts", "children", "vars"].index), "порядок розділів: хости, children, vars"
    servers = ds["args"][0]
    first = text.lstrip().splitlines()[0]
    if loose:
        assert not first.startswith("["), "хости без груп мають стояти на початку, до перших розділів"
    for s in servers:   # порядок ключів у рядку вузла
        for g in s.get("groups") or []:
            tok = hostvars[s["name"]]["_line_" + g]
            keys = [t.split("=")[0] for t in tok[1:]]
            extra = sorted((s.get("vars") or {}))
            want = ["ansible_host", "ansible_user"] + (["ansible_port"] if "port" in s else []) + extra
            assert keys == want, f"порядок змінних вузла {s['name']}: {keys}, очікується {want}"
    assert text.endswith("\n") and not text.endswith("\n\n"), "текст має закінчуватися одним переведенням рядка"


def c1_real(ds, text, sbx):
    need_ansible()
    sbx.write("lab.ini", text)
    rc, out = sbx.run(["ansible-inventory", "-i", "lab.ini", "--list"])
    assert rc == 0, "ansible-inventory не прочитав інвентар:\n" + out[-500:]
    data = json.loads(out)
    _, closure, hv = inv_expect(ds["args"])

    def all_hosts(g):
        node = data.get(g, {})
        hosts = set(node.get("hosts", []))
        for c in node.get("children", []):
            hosts |= all_hosts(c)
        return hosts
    for g, want in closure.items():
        assert all_hosts(g) == want, f"група {g}: {all_hosts(g)}, очікується {want}"
    meta = data["_meta"]["hostvars"]
    for name, want in hv.items():
        for k, v in want.items():
            assert str(meta[name][k]) == v, f"{name}.{k} = {meta[name].get(k)}, очікується {v}"


CHECKS[1] = [("model", c1_model, False), ("hostvars", c1_hostvars, False), ("layout", c1_layout, False),
             ("real", c1_real, True)]
DATA[1] = [dict(tag="S1", set=1, args=INV_S1), dict(tag="S2", set=2, args=INV_S2), dict(tag="S3", set=3, args=INV_S3)]


# ---------------------------------------------------------------------------------------------
# Задача 2. Ad-hoc
# ---------------------------------------------------------------------------------------------
def parse_adhoc(cmd):
    t = shlex.split(cmd)
    assert t[0] == "ansible", "команда має починатися зі слова ansible"
    d = {"pattern": t[1], "flags": [], "order": []}
    i = 2
    while i < len(t):
        a = t[i]
        if a in ("-i", "-m", "-a", "--limit", "--forks", "-e"):
            d[a] = t[i + 1]
            d["order"].append(a)
            i += 2
        else:
            d["flags"].append(a)
            d["order"].append(a)
            i += 1
    return d


def kv(text):
    out = {}
    for tok in shlex.split(text):
        k, v = tok.split("=", 1)
        out[k] = v
    return out


def _adhoc_expect(spec):
    m = spec["module"]
    return m if "." in m else "ansible.builtin." + m


def c2_core(ds, text):
    spec = ds["args"][0]
    d = parse_adhoc(text)
    assert d["pattern"] == spec["pattern"]
    assert d["-i"] == spec.get("inventory", "inventory.ini")
    assert d["-m"] == _adhoc_expect(spec), f"модуль {d['-m']}, очікується {_adhoc_expect(spec)}"
    assert "\n" not in text.strip()


def c2_args(ds, text):
    spec = ds["args"][0]
    d = parse_adhoc(text)
    if spec.get("args"):
        a = spec["args"]
        want = kv(a) if isinstance(a, str) else {k: str(v) for k, v in a.items()}
        assert kv(d["-a"]) == want, f"аргументи модуля {kv(d['-a'])}, очікується {want}"
    else:
        assert "-a" not in d, "без аргументів прапорець -a не потрібен"


def c2_flags(ds, text):
    spec = ds["args"][0]
    d = parse_adhoc(text)
    for flag, key in (("--become", "become"), ("--check", "check"), ("--diff", "diff")):
        assert (flag in d["flags"]) == bool(spec.get(key)), f"прапорець {flag}: {flag in d['flags']}"
    assert not set(d["flags"]) - {"--become", "--check", "--diff"}, f"зайві прапорці: {d['flags']}"
    if spec.get("limit"):
        assert d["--limit"] == spec["limit"]
    else:
        assert "--limit" not in d
    if spec.get("forks"):
        assert d["--forks"] == str(spec["forks"])
    else:
        assert "--forks" not in d
    if spec.get("extra_vars"):
        a = spec["extra_vars"]
        want = kv(a) if isinstance(a, str) else {k: str(v) for k, v in a.items()}
        assert kv(d["-e"]) == want
    else:
        assert "-e" not in d


def c2_order(ds, text):
    d = parse_adhoc(text)
    want = ["-i", "-m", "-a", "--become", "--check", "--diff", "--limit", "--forks", "-e"]
    got = [o for o in d["order"]]
    assert got == sorted(got, key=want.index), f"порядок параметрів: {got}"
    assert got[:2] == ["-i", "-m"]


def c2_real(ds, text, sbx):
    need_ansible()
    spec = ds["args"][0]
    if spec.get("become"):
        pytest.skip("перевірку з --become не запускають: потрібні права адміністратора")
    pat = spec["pattern"]
    rc, hosts_out = sbx.run(["ansible", pat, "-i", "inventory.ini", "--list-hosts"]
                            + (["--limit", spec["limit"]] if spec.get("limit") else []))
    assert rc == 0, hosts_out
    hosts = [h.strip() for h in hosts_out.splitlines()[1:] if h.strip()]
    rc, out = sbx.run(text, shell=True)
    assert rc == 0, "команда завершилась з помилкою:\n" + out[-500:]
    if spec["module"].endswith("ping"):
        assert out.count("pong") == len(hosts) > 0, out
    a = spec.get("args")
    if isinstance(a, dict) and spec["module"].split(".")[-1] in ("file", "copy"):
        target = a.get("dest") or a.get("path")
        for h in hosts:
            exists = (sbx.dir / target.replace("{{ inventory_hostname }}", h)).exists()
            assert exists == (not spec.get("check")), f"{h}: файл {target} existує={exists}, check={spec.get('check')}"
    if spec.get("check") and spec.get("diff"):
        assert "+++" in out or "dest" in out, "у режимі --check --diff очікується показ різниці"


def _s2(**kw):
    kw.setdefault("inventory", "inventory.ini")
    return dict(kw)


ADHOC_S1 = _s2(pattern="web", module="ping", forks=5)
ADHOC_S2 = _s2(pattern="db:&prod", module="copy", args={"dest": "report/{{ inventory_hostname }}.txt",
                                                       "content": "перевірка зв'язку"}, check=True, diff=True)
ADHOC_S3 = _s2(pattern="all:!db", module="ansible.builtin.file", args={"path": "marker-{{ inventory_hostname }}",
                                                                       "state": "touch"}, limit="web1",
               extra_vars={"owner": "student"})
CHECKS[2] = [("core", c2_core, False), ("args", c2_args, False), ("flags", c2_flags, False),
             ("order", c2_order, False), ("real", c2_real, True)]
DATA[2] = [dict(tag="S1", set=1, args=(ADHOC_S1,)), dict(tag="S2", set=2, args=(ADHOC_S2,)),
           dict(tag="S3", set=3, args=(ADHOC_S3,))]


# ---------------------------------------------------------------------------------------------
# Задача 3. Вебсервер
# ---------------------------------------------------------------------------------------------
WEB_S1 = dict(group="web", package="nginx", service="nginx", manager="apt", port=80, docroot="/var/www/html",
              template="nginx.conf.j2", config_path="/etc/nginx/nginx.conf", reload=False)
WEB_S2 = dict(group="frontend", package="apache2", service="apache2", manager="apt", port=8080,
              docroot="/var/www/shop", template="apache.conf.j2", config_path="/etc/apache2/ports.conf", reload=True)
WEB_S3 = dict(group="edge", package="nginx", service="nginx", manager="dnf", port=8081,
              docroot="/usr/share/nginx/portal", template="portal.conf.j2",
              config_path="/etc/nginx/conf.d/portal.conf", reload=True)


def _handler_name(cfg):
    return ("Перезавантажити " if cfg["reload"] else "Перезапустити ") + cfg["service"]


def c3_play(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    assert p.get("name"), "гра має мати назву"
    assert p["hosts"] == cfg["group"]
    assert truthy(p.get("become")), "для встановлення пакетів потрібен become: true"
    v = p.get("vars") or {}
    assert v.get("web_port") == cfg["port"] and v.get("web_docroot") == cfg["docroot"], f"змінні гри: {v}"


def c3_tasks(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    tasks = p["tasks"]
    mod = "ansible.builtin." + cfg["manager"]
    pk = [t for t in tasks if module_of(t) == mod]
    assert len(pk) == 1, f"потрібне одне завдання модуля {mod}"
    prm = pk[0][mod]
    assert prm["name"] == cfg["package"] and prm["state"] in ("present", "installed")
    if cfg["manager"] == "apt":
        assert truthy(prm.get("update_cache")), "для apt потрібен update_cache: true"
    d = [t for t in find_tasks(tasks, "file")]
    assert d and mod_params(d[0])["state"] == "directory"
    assert str(mod_params(d[0])["path"]) in (cfg["docroot"], "{{ web_docroot }}")
    tp = find_tasks(tasks, "template")
    assert len(tp) == 1
    prm = mod_params(tp[0])
    assert prm["src"] == cfg["template"] and prm["dest"] == cfg["config_path"]
    assert tp[0].get("notify") in (_handler_name(cfg), [_handler_name(cfg)]), f"notify: {tp[0].get('notify')}"
    sv = find_tasks(tasks, "service")
    assert len(sv) == 1
    prm = mod_params(sv[0])
    assert prm["name"] == cfg["service"] and prm["state"] == "started" and truthy(prm["enabled"])
    order = [module_of(t) for t in tasks]
    assert order.index(mod) < order.index("ansible.builtin.template") < order.index("ansible.builtin.service"), order


def c3_handler(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    hs = p.get("handlers") or []
    assert len(hs) == 1, "потрібен один обробник"
    h = hs[0]
    assert h["name"] == _handler_name(cfg)
    prm = mod_params(h)
    assert module_of(h) == "ansible.builtin.service" and prm["name"] == cfg["service"]
    assert prm["state"] == ("reloaded" if cfg["reload"] else "restarted")


def c3_style(ds, text):
    p = only_play(text)
    for t in all_tasks(p) + list(p.get("handlers") or []):
        assert t.get("name"), f"завдання без назви: {t}"
        m = module_of(t)
        assert m.startswith("ansible.builtin."), f"потрібне повне ім'я модуля (FQCN): {m}"
        assert m.split(".")[-1] not in ("command", "shell", "raw", "script"), "command/shell тут не потрібні"
        for val in (mod_params(t) if isinstance(mod_params(t), dict) else {}).values():
            assert val != "latest", "state: latest не ідемпотентний щодо версій; використайте present"


def c3_syntax(ds, text, sbx):
    need_ansible()
    syntax_check(sbx, text)


CHECKS[3] = [("play", c3_play, False), ("tasks", c3_tasks, False), ("handler", c3_handler, False),
             ("style", c3_style, False), ("syntax", c3_syntax, True)]
DATA[3] = [dict(tag="S1", set=1, args=(WEB_S1,)), dict(tag="S2", set=2, args=(WEB_S2,)),
           dict(tag="S3", set=3, args=(WEB_S3,))]


# ---------------------------------------------------------------------------------------------
# Задача 4. PostgreSQL
# ---------------------------------------------------------------------------------------------
PG_S1 = dict(group="db", version="16", db_name="shop", db_user="shop_app", password_var="vault_shop_password",
             listen="localhost", max_connections=100, cidr="10.20.1.0/24")
PG_S2 = dict(group="journal", version="15", db_name="journal", db_user="journal_rw", password_var="vault_journal_pw",
             listen="*", max_connections=200, cidr="192.168.10.0/24")
PG_S3 = dict(group="library", version="16", db_name="library", db_user="lib_reader", password_var="vault_lib_pass",
             listen="10.20.1.31", max_connections=50, cidr="10.20.2.0/24")


def _lines(tasks, path_end):
    return [mod_params(t) for t in find_tasks(tasks, "lineinfile") if str(mod_params(t)["path"]).endswith(path_end)]


def c4_config(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    t = p["tasks"]
    ap = find_tasks(t, "apt")
    assert ap and mod_params(ap[0])["name"] == f"postgresql-{cfg['version']}" and mod_params(ap[0])["state"] == "present"
    base = f"/etc/postgresql/{cfg['version']}/main/"
    conf = _lines(t, "postgresql.conf")
    assert len(conf) == 2 and all(c["path"] == base + "postgresql.conf" for c in conf)
    lines = {" ".join(c["line"].split()) for c in conf}
    assert lines == {f"listen_addresses = '{cfg['listen']}'", f"max_connections = {cfg['max_connections']}"}, lines
    assert all(c.get("regexp") for c in conf), "lineinfile без regexp додає рядок щоразу заново"
    hba = _lines(t, "pg_hba.conf")
    assert len(hba) == 1 and hba[0]["path"] == base + "pg_hba.conf"
    assert " ".join(hba[0]["line"].split()) == f"host {cfg['db_name']} {cfg['db_user']} {cfg['cidr']} scram-sha-256"
    assert hba[0].get("regexp")
    assert truthy(p.get("become"))
    assert p["hosts"] == cfg["group"]


def c4_service(ds, text):
    p = only_play(text)
    t = p["tasks"]
    sv = [mod_params(x) for x in find_tasks(t, "service")]
    assert any(s["name"] == "postgresql" and s["state"] == "started" and truthy(s["enabled"]) for s in sv)
    hs = p.get("handlers") or []
    assert len(hs) == 1 and mod_params(hs[0])["name"] == "postgresql" and mod_params(hs[0])["state"] == "restarted"
    for x in find_tasks(t, "lineinfile"):
        assert x.get("notify") in (hs[0]["name"], [hs[0]["name"]]), f"завдання «{x['name']}» не сповіщає обробник"


def c4_guards(ds, text):
    p = only_play(text)
    regs, checked = set(), 0
    for t in p["tasks"]:
        m = module_of(t)
        if m.split(".")[-1] in ("command", "shell"):
            body = str(t[m])
            assert t.get("become_user") == "postgres", f"«{t['name']}»: psql/createdb виконує користувач postgres"
            if t.get("changed_when") is False:
                assert t.get("register"), "перевірка без register марна"
                assert t.get("check_mode") is False, "читальні перевірки мають працювати і в режимі --check"
                regs.add(t["register"])
                checked += 1
            else:
                cond = str(t.get("when", ""))
                assert any(r in cond for r in regs), f"«{t['name']}»: потрібен when за результатом перевірки"
    assert checked == 2, f"очікуються 2 читальні перевірки (роль, база), знайдено {checked}"


def c4_secrets(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    users = [t for t in p["tasks"] if cfg["password_var"] in str(t)]
    assert len(users) == 1, "пароль має використовуватися рівно в одному завданні"
    assert users[0].get("no_log") is True, "завдання з паролем має мати no_log: true"
    assert "{{ " + cfg["password_var"] + " }}" in str(users[0]) or "{{" + cfg["password_var"] + "}}" in str(users[0])
    assert re.search(r"PASSWORD\s+'\{\{", yaml.safe_dump(users[0], allow_unicode=True)), "пароль лише через змінну"
    assert "PASSWORD '" not in re.sub(r"PASSWORD '\{\{[^}]*\}\}'", "", text)


def c4_syntax(ds, text, sbx):
    need_ansible()
    syntax_check(sbx, text)


CHECKS[4] = [("config", c4_config, False), ("service", c4_service, False), ("guards", c4_guards, False),
             ("secrets", c4_secrets, False), ("syntax", c4_syntax, True)]
DATA[4] = [dict(tag="S1", set=1, args=(PG_S1,)), dict(tag="S2", set=2, args=(PG_S2,)),
           dict(tag="S3", set=3, args=(PG_S3,))]


# ---------------------------------------------------------------------------------------------
# Задача 5. Ідемпотентні заміни
# ---------------------------------------------------------------------------------------------
HANDLER5 = [{"name": "Повідомити", "ansible.builtin.debug": {"msg": "Журнали підготовлено"}}]


def _play5(tasks, handlers=None):
    play = {"name": "Налаштування вузла", "hosts": "app", "gather_facts": False, "tasks": tasks}
    if handlers:
        play["handlers"] = handlers
    return yaml.safe_dump([play], allow_unicode=True, sort_keys=False, width=1000)


R = "{{ root }}"
IDM_S1_TASKS = [
    {"name": "Каталог сайту", "ansible.builtin.command": f'mkdir -p "{R}/srv/site/logs"'},
    {"name": "Початковий текст", "ansible.builtin.shell": f'echo "Сайт коледжу" >> "{R}/srv/site/index.txt"'},
    {"name": "Позначка готовності", "ansible.builtin.command": f'touch "{R}/srv/site/.ready"', "tags": ["init"]},
    {"name": "Права на журнали", "ansible.builtin.command": f'chmod 750 "{R}/srv/site/logs"'},
    {"name": "Тип системи", "ansible.builtin.command": "uname -s", "changed_when": False},
]
IDM_S1_EXPECT = {
    "Каталог сайту": ("ansible.builtin.file", {"path": f"{R}/srv/site/logs", "state": "directory"}),
    "Початковий текст": ("ansible.builtin.lineinfile", {"path": f"{R}/srv/site/index.txt", "line": "Сайт коледжу",
                                                         "create": True}),
    "Позначка готовності": ("ansible.builtin.file", {"path": f"{R}/srv/site/.ready", "state": "touch",
                                                      "modification_time": "preserve", "access_time": "preserve"}),
    "Права на журнали": ("ansible.builtin.file", {"path": f"{R}/srv/site/logs", "mode": "0750"}),
    "Тип системи": None,
}
IDM_S1_FILES = {"srv/site/index.txt": "Сайт коледжу\n", "srv/site/.ready": "", "srv/site/logs": None}

IDM_S2_TASKS = [
    {"name": "Каталог застосунку", "command": f'mkdir "{R}/etc"'},
    {"name": "Каталог налаштувань", "command": f'mkdir -p "{R}/etc/app"'},
    {"name": "Рівень журналу", "shell": f'echo "log_level = info" >> "{R}/etc/app/app.conf"'},
    {"name": "Ліміт клієнтів", "shell": f'echo max_clients = 50 >> "{R}/etc/app/app.conf"'},
    {"name": "Резервна копія", "command": f'cp "{R}/etc/app/app.conf" "{R}/etc/app/app.conf.bak"',
     "when": "make_copy | default(true)"},
    {"name": "Посилання на поточний файл", "command": f'ln -s "{R}/etc/app/app.conf" "{R}/etc/current.conf"'},
    {"name": "Прибрати застарілий файл", "command": f'rm -f "{R}/etc/app/stale.conf"'},
]
IDM_S2_EXPECT = {
    "Каталог застосунку": ("ansible.builtin.file", {"path": f"{R}/etc", "state": "directory"}),
    "Каталог налаштувань": ("ansible.builtin.file", {"path": f"{R}/etc/app", "state": "directory"}),
    "Рівень журналу": ("ansible.builtin.lineinfile", {"path": f"{R}/etc/app/app.conf", "line": "log_level = info",
                                                      "create": True}),
    "Ліміт клієнтів": ("ansible.builtin.lineinfile", {"path": f"{R}/etc/app/app.conf", "line": "max_clients = 50",
                                                      "create": True}),
    "Резервна копія": ("ansible.builtin.copy", {"src": f"{R}/etc/app/app.conf", "dest": f"{R}/etc/app/app.conf.bak",
                                                 "remote_src": True}),
    "Посилання на поточний файл": ("ansible.builtin.file", {"src": f"{R}/etc/app/app.conf",
                                                             "dest": f"{R}/etc/current.conf", "state": "link"}),
    "Прибрати застарілий файл": ("ansible.builtin.file", {"path": f"{R}/etc/app/stale.conf", "state": "absent"}),
}
IDM_S2_FILES = {"etc/app/app.conf": "log_level = info\nmax_clients = 50\n",
                "etc/app/app.conf.bak": "log_level = info\nmax_clients = 50\n", "etc/app": None}

IDM_S3_TASKS = [
    {"name": "Каталоги журналів", "ansible.builtin.command": f'mkdir -p "{R}/var/log/app"', "notify": "Повідомити"},
    {"name": "Файл журналу", "command": f'touch "{R}/var/log/app/app.log"'},
    {"name": "Заголовок журналу", "ansible.builtin.shell": f'echo "# журнал застосунку" >> "{R}/var/log/app/app.log"'},
    {"name": "Права на журнал", "command": f'chmod 640 "{R}/var/log/app/app.log"'},
    {"name": "Одноразова ініціалізація", "ansible.builtin.command": f'touch "{R}/var/.initialized"',
     "args": {"creates": f"{R}/var/.initialized"}},
    {"name": "Кількість файлів", "ansible.builtin.shell": f'ls "{R}/var" | wc -l', "changed_when": False},
]
IDM_S3_EXPECT = {
    "Каталоги журналів": ("ansible.builtin.file", {"path": f"{R}/var/log/app", "state": "directory"}),
    "Файл журналу": ("ansible.builtin.file", {"path": f"{R}/var/log/app/app.log", "state": "touch",
                                              "modification_time": "preserve", "access_time": "preserve"}),
    "Заголовок журналу": ("ansible.builtin.lineinfile", {"path": f"{R}/var/log/app/app.log",
                                                         "line": "# журнал застосунку", "create": True}),
    "Права на журнал": ("ansible.builtin.file", {"path": f"{R}/var/log/app/app.log", "mode": "0640"}),
    "Одноразова ініціалізація": None,
    "Кількість файлів": None,
}
IDM_S3_FILES = {"var/log/app/app.log": "# журнал застосунку\n", "var/.initialized": "", "var/log/app": None}


def _idm(tasks, expect, files, handlers=None):
    return dict(playbook=_play5(tasks, handlers), expect=expect, files=files, tasks=tasks, handlers=handlers)


IDM_S1, IDM_S2, IDM_S3 = _idm(IDM_S1_TASKS, IDM_S1_EXPECT, IDM_S1_FILES), _idm(IDM_S2_TASKS, IDM_S2_EXPECT, IDM_S2_FILES), \
    _idm(IDM_S3_TASKS, IDM_S3_EXPECT, IDM_S3_FILES, HANDLER5)


def _tasks5(text):
    return only_play(text)["tasks"]


def c5_rewrite(ds, text):
    spec = ds["extra"]
    got = {t["name"]: t for t in _tasks5(text)}
    assert list(got) == [t["name"] for t in spec["tasks"]], "кількість і порядок завдань мають зберегтися"
    for name, exp in spec["expect"].items():
        if exp is None:
            continue
        assert module_of(got[name]) == exp[0], f"«{name}»: модуль {module_of(got[name])}, очікується {exp[0]}"


def c5_params(ds, text):
    spec = ds["extra"]
    got = {t["name"]: t for t in _tasks5(text)}
    for name, exp in spec["expect"].items():
        if exp is None:
            continue
        have = mod_params(got[name])
        norm = {k: (str(v) if k == "mode" else v) for k, v in have.items()}
        assert norm == exp[1], f"«{name}»: параметри {norm}, очікується {exp[1]}"


def c5_untouched(ds, text):
    spec = ds["extra"]
    got = {t["name"]: t for t in _tasks5(text)}
    for orig in spec["tasks"]:
        name = orig["name"]
        new = got[name]
        if spec["expect"][name] is None:
            assert new == orig, f"«{name}» не мало змінитися: {new}"
        else:
            keep = lambda d: {k: v for k, v in d.items() if k in KEYWORDS}
            assert keep(new) == keep(orig), f"«{name}»: службові ключі (when, tags, notify) мають зберегтися"
            assert list(new)[0] == "name"
    p = only_play(text)
    assert p["hosts"] == "app" and p.get("gather_facts") is False
    assert p.get("handlers") == spec.get("handlers"), "обробники мають залишитися без змін"


def c5_residual(ds, text):
    for t in _tasks5(text):
        m = module_of(t)
        if m.split(".")[-1] in ("command", "shell") and "args" not in t:
            body = str(t[m]).split()
            assert body[0] not in ("mkdir", "touch", "chmod", "ln", "rm", "cp") and ">>" not in body, \
                f"лишилася неідемпотентна команда: {t[m]}"


def c5_real(ds, text, sbx):
    need_ansible()
    spec = ds["extra"]
    sbx.write("fixed.yml", text)
    rc, out1 = sbx.play("fixed.yml")
    assert rc == 0, "перший запуск не вдався:\n" + out1[-600:]
    rc, out2 = sbx.play("fixed.yml")
    assert rc == 0, out2[-400:]
    assert total_changed(out2) == 0, "другий запуск має давати changed=0:\n" + out2[-700:]
    base = sbx.dir / "nodes" / "node1"
    for rel, content in spec["files"].items():
        p = base / rel
        assert p.exists(), f"немає {rel}"
        if content is None:
            assert p.is_dir()
        else:
            assert p.read_text(encoding="utf-8") == content, f"{rel}: {p.read_text(encoding='utf-8')!r}"
    if "etc/app" in spec["files"]:
        assert (base / "etc/current.conf").is_symlink()
        assert not (base / "etc/app/stale.conf").exists()
    if "srv/site/logs" in spec["files"]:
        assert oct((base / "srv/site/logs").stat().st_mode & 0o777) == "0o750"
    if "var/log/app" in spec["files"]:
        assert oct((base / "var/log/app/app.log").stat().st_mode & 0o777) == "0o640"


CHECKS[5] = [("rewrite", c5_rewrite, False), ("params", c5_params, False), ("untouched", c5_untouched, False),
             ("residual", c5_residual, False), ("real", c5_real, True)]
DATA[5] = [dict(tag="S1", set=1, args=(IDM_S1["playbook"],), extra=IDM_S1),
           dict(tag="S2", set=2, args=(IDM_S2["playbook"],), extra=IDM_S2),
           dict(tag="S3", set=3, args=(IDM_S3["playbook"],), extra=IDM_S3)]


# ---------------------------------------------------------------------------------------------
# Задача 6. Порівняння запусків
# ---------------------------------------------------------------------------------------------
def recap_text(rows, plays=1):
    """Синтетичний вивід ansible-playbook із заданим підсумком {хост: (ok, changed, unreachable, failed, skipped)}."""
    out = []
    for i in range(plays):
        out += [f"PLAY [Гра {i + 1}] " + "*" * 40, "", "TASK [Gathering Facts] " + "*" * 30]
        out += [f"ok: [{h}]" for h in rows]
        out += ["", "PLAY RECAP " + "*" * 50]
        for h, (ok, ch, un, fa, sk) in rows.items():
            out.append(f"{h:<27}: ok={ok:<4} changed={ch:<4} unreachable={un:<4} failed={fa:<4} skipped={sk:<4} "
                       f"rescued=0    ignored=0   ")
        out.append("")
    return "\n".join(out)


def expected_runs(a, b):
    hosts = {}
    for h in sorted(set(a) | set(b)):
        if h not in a or h not in b:
            v = "немає в одному із запусків"
            c1, c2 = (a.get(h) or (0, 0))[1], (b.get(h) or (0, 0))[1]
        else:
            c1, c2 = a[h][1], b[h][1]
            if a[h][2] or b[h][2]:
                v = "недоступний"
            elif a[h][3] or b[h][3]:
                v = "помилка"
            else:
                v = "ідемпотентний" if c2 == 0 else "не ідемпотентний"
        hosts[h] = {"changed_first": c1, "changed_second": c2, "verdict": v}
    return {"hosts": hosts, "idempotent": bool(hosts) and all(x["verdict"] == "ідемпотентний" for x in hosts.values()),
            "changed_second_total": sum(x["changed_second"] for x in hosts.values())}


RUN_S1 = ({"web1": (4, 3, 0, 0, 0), "web2": (4, 3, 0, 0, 0)}, {"web1": (4, 0, 0, 0, 0), "web2": (4, 0, 0, 0, 0)})
RUN_S2 = ({"web1": (5, 4, 0, 0, 0), "db1": (5, 4, 0, 0, 1)}, {"web1": (5, 0, 0, 0, 1), "db1": (5, 1, 0, 0, 1)})
RUN_S3 = ({"app1": (6, 5, 0, 0, 0), "app2": (0, 0, 1, 0, 0), "app3": (3, 2, 0, 1, 0), "app4": (6, 5, 0, 0, 0)},
          {"app1": (6, 0, 0, 0, 0), "app2": (0, 0, 1, 0, 0), "app3": (3, 0, 0, 1, 0), "app5": (6, 0, 0, 0, 0)})


def _run_args(rows, plays=1):
    return (recap_text(rows[0], plays), recap_text(rows[1], plays))


def c6_hosts(ds, res):
    exp = expected_runs(*ds["extra"])
    assert isinstance(res, dict) and set(res) == {"hosts", "idempotent", "changed_second_total"}, f"ключі: {set(res)}"
    assert res["hosts"] == exp["hosts"], f"hosts: {res['hosts']}, очікується {exp['hosts']}"


def c6_totals(ds, res):
    exp = expected_runs(*ds["extra"])
    assert res["idempotent"] is exp["idempotent"]
    assert res["changed_second_total"] == exp["changed_second_total"]


def c6_types(ds, res):
    for h, v in res["hosts"].items():
        assert set(v) == {"changed_first", "changed_second", "verdict"}
        assert isinstance(v["changed_first"], int) and isinstance(v["changed_second"], int)
        assert list(res["hosts"]) == sorted(res["hosts"]), "хости мають іти за абеткою"


def c6_real(ds, res, sbx, mod):
    need_ansible()
    good = _play5([{"name": "Каталог", "ansible.builtin.file": {"path": f"{R}/d", "state": "directory"}},
                   {"name": "Рядок", "ansible.builtin.lineinfile": {"path": f"{R}/d/f", "line": "x", "create": True}}])
    good = good.replace("hosts: app", "hosts: web")
    bad = yaml.safe_dump([{"name": "Невдалий", "hosts": "web", "gather_facts": False, "tasks": [
        {"name": "Каталог", "ansible.builtin.file": {"path": f"{R}/d", "state": "directory"}},
        {"name": "Мітка", "ansible.builtin.command": f"touch {R}/d/t"}]}], allow_unicode=True, sort_keys=False)
    fn = getattr(mod, FUNC[6])
    for name, text, want in (("good", good, "ідемпотентний"), ("bad", bad, "не ідемпотентний")):
        sbx.write(name + ".yml", text)
        rc1, o1 = sbx.play(name + ".yml")
        rc2, o2 = sbx.play(name + ".yml")
        assert rc1 == 0 and rc2 == 0, o2[-400:]
        r = fn(o1, o2)
        assert set(r["hosts"]) == {"web1", "web2"}
        assert all(v["verdict"] == want for v in r["hosts"].values()), f"{name}: {r}"
        assert r["idempotent"] is (want == "ідемпотентний")
        assert r["changed_second_total"] == total_changed(o2)


CHECKS[6] = [("hosts", c6_hosts, False), ("totals", c6_totals, False), ("types", c6_types, False),
             ("real", c6_real, True)]
DATA[6] = [dict(tag="S1", set=1, args=_run_args(RUN_S1), extra=RUN_S1),
           dict(tag="S2", set=2, args=_run_args(RUN_S2), extra=RUN_S2),
           dict(tag="S3", set=3, args=_run_args(RUN_S3), extra=RUN_S3)]


# ---------------------------------------------------------------------------------------------
# Задача 7. Шаблон Jinja2
# ---------------------------------------------------------------------------------------------
TPL_S1 = dict(kind="nginx", banner="Керується Ansible. Ручні зміни буде перезаписано.", sites_var="sites",
              port_var="web_port", default_root="/var/www/html", tls_var="enable_tls")
TPL_S2 = dict(kind="apache", banner="Віртуальні вузли коледжу; не редагувати вручну", sites_var="vhosts",
              port_var="http_port", default_root="/srv/www")
TPL_S3 = dict(kind="pg_hba", banner="Доступ до PostgreSQL; файл керується Ansible", rules_var="rules",
              db_var="db_name", default_method="scram-sha-256", strict_var="close_rest")
CTX_S1 = [
    {"web_port": 8080, "enable_tls": False,
     "sites": [{"name": "college.example", "aliases": ["www.college.example"], "root": "/srv/college"},
               {"name": "lib.example"}]},
    {"web_port": 80, "enable_tls": True, "sites": [{"name": "portal.example", "aliases": ["a.example", "b.example"]}]},
    {"web_port": 80, "enable_tls": True, "sites": []},
]
CTX_S2 = [
    {"http_port": 8080, "vhosts": [{"name": "shop.example", "aliases": ["www.shop.example", "m.shop.example"],
                                     "root": "/srv/shop"}, {"name": "blog.example"}]},
    {"http_port": 81, "vhosts": [{"name": "only.example", "aliases": []}]},
    {"http_port": 8080, "vhosts": []},
]
CTX_S3 = [
    {"db_name": "shop", "close_rest": True, "rules": [{"user": "shop_app", "cidr": "10.20.1.0/24"},
                                                      {"user": "report", "cidr": "10.20.5.7/32", "method": "md5"}]},
    {"db_name": "journal", "close_rest": False, "rules": [{"user": "teacher", "cidr": "192.168.10.0/24"}]},
    {"db_name": "library", "close_rest": True, "rules": []},
]


def expected_render(cfg, ctx):
    k = cfg["kind"]
    out = ["# " + cfg["banner"]]
    if k == "nginx":
        for s in ctx[cfg["sites_var"]]:
            names = " ".join([s["name"]] + list(s.get("aliases", [])))
            out += ["server {", f"    listen {ctx[cfg['port_var']]};", f"    server_name {names};",
                    f"    root {s.get('root', cfg['default_root'])};"]
            if ctx[cfg["tls_var"]]:
                out.append(f"    ssl_certificate /etc/ssl/{s['name']}.crt;")
            out.append("}")
    elif k == "apache":
        port = ctx[cfg["port_var"]]
        out.append(f"Listen {port}")
        for s in ctx[cfg["sites_var"]]:
            out += [f"<VirtualHost *:{port}>", f"    ServerName {s['name']}"]
            if s.get("aliases"):
                out.append("    ServerAlias " + " ".join(s["aliases"]))
            out += [f"    DocumentRoot {s.get('root', cfg['default_root'])}", "</VirtualHost>"]
    else:
        for r in ctx[cfg["rules_var"]]:
            out.append(f"host {ctx[cfg['db_var']]} {r['user']} {r['cidr']} {r.get('method', cfg['default_method'])}")
        if ctx[cfg["strict_var"]]:
            out.append("host all all 0.0.0.0/0 reject")
    return [" ".join(x.split()) for x in out]


def render_jinja(text, ctx):
    import jinja2
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    return env.from_string(text).render(**ctx)


def c7_render(ds, text):
    cfg = ds["args"][0]
    for ctx in ds["extra"]:
        try:
            out = render_jinja(text, ctx)
        except Exception as e:
            raise AssertionError(f"шаблон не відтворюється з даними {ctx}: {type(e).__name__}: {e}")
        assert squash(out) == expected_render(cfg, ctx), f"дані {ctx}:\n{out}"


def c7_dynamic(ds, text):
    cfg = ds["args"][0]
    assert "{%" in text and "for" in text, "шаблон має містити цикл Jinja2"
    for ctx in ds["extra"]:
        for key in ("sites", "vhosts", "rules"):
            for item in ctx.get(key, []):
                for v in (item.get("name"), item.get("user"), item.get("cidr")):
                    assert not v or v not in text, f"значення «{v}» вписано в шаблон замість змінної"
    assert cfg["banner"] in text and text.startswith("#"), "перший рядок: коментар із текстом банера"


def c7_edge(ds, text):
    cfg = ds["args"][0]
    ctx = copy.deepcopy(ds["extra"][-1])
    out = squash(render_jinja(text, ctx))
    assert out == expected_render(cfg, ctx)
    assert out[0] == "# " + cfg["banner"]


def c7_real(ds, text, sbx):
    need_ansible()
    cfg = ds["args"][0]
    sbx.write("templates/t.j2", text)
    play = yaml.safe_dump([{"name": "Шаблон", "hosts": "node1", "gather_facts": False, "tasks": [
        {"name": "Каталог вузла", "ansible.builtin.file": {"path": R, "state": "directory"}},
        {"name": "Конфігурація", "ansible.builtin.template": {"src": "t.j2", "dest": f"{R}/out.conf"}}]}],
        allow_unicode=True, sort_keys=False)
    sbx.write("t.yml", play)
    ctx1, ctx2 = ds["extra"][0], ds["extra"][1]
    sbx.write("v1.json", json.dumps(ctx1))
    sbx.write("v2.json", json.dumps(ctx2))
    rc, o = sbx.play("t.yml", "-e", "@v1.json")
    assert rc == 0, o[-600:]
    out_file = sbx.dir / "nodes/node1/out.conf"
    assert squash(out_file.read_text(encoding="utf-8")) == expected_render(cfg, ctx1)
    rc, o = sbx.play("t.yml", "-e", "@v1.json")
    assert total_changed(o) == 0, "повторний запуск не має змінювати файл"
    before = out_file.read_text(encoding="utf-8")
    rc, o = sbx.play("t.yml", "-e", "@v2.json", "--check", "--diff")
    assert rc == 0 and total_changed(o) == 1 and "---" in o, o[-500:]
    assert out_file.read_text(encoding="utf-8") == before, "режим --check не повинен змінювати файл"


CHECKS[7] = [("render", c7_render, False), ("dynamic", c7_dynamic, False), ("edge", c7_edge, False),
             ("real", c7_real, True)]
DATA[7] = [dict(tag="S1", set=1, args=(TPL_S1,), extra=CTX_S1), dict(tag="S2", set=2, args=(TPL_S2,), extra=CTX_S2),
           dict(tag="S3", set=3, args=(TPL_S3,), extra=CTX_S3)]


# ---------------------------------------------------------------------------------------------
# Задача 8. Розбір --check --diff
# ---------------------------------------------------------------------------------------------
def diff_output(items, noise=True):
    """Синтетичний вивід ansible-playbook --check --diff.
    items: список (завдання, вузол, шлях або None для модуля file, [(знак, рядок)...])."""
    out = ["PLAY [Оновлення конфігурації] " + "*" * 30]
    task = None
    for t, host, path, body in items:
        if t != task:
            task = t
            out += ["", f"TASK [{t}] " + "*" * 40]
        if path is None:
            out += ["--- before", "+++ after", "@@ -1,4 +1,4 @@", " {", f'     "path": "nodes/{host}/etc",',
                    '-    "state": "absent"', '+    "state": "directory"', " }", ""]
        else:
            suffix = " (content)" if path.endswith(".txt") else ""
            after = path if suffix else "/tmp/ansible-local/tmpabc/app.conf.j2"
            old = sum(1 for sign, _ in body if sign in " -")
            new = sum(1 for sign, _ in body if sign in " +")
            out += [f"--- before: {path}{suffix}", f"+++ after: {after}{suffix}", f"@@ -1,{old} +1,{new} @@"]
            out += [f"{sign}{line}" for sign, line in body]
            out.append("")
        out.append(f"changed: [{host}]")
    if noise:
        out += ["", "RUNNING HANDLER [Перезапустити службу] " + "*" * 20, "skipping: [web1]", "",
                "PLAY RECAP " + "*" * 40, "web1                       : ok=3    changed=2    unreachable=0    "
                "failed=0    skipped=1    rescued=0    ignored=0   "]
    return "\n".join(out) + "\n"


def expected_diff(items):
    res = []
    for t, host, path, body in items:
        if path is None:
            continue
        res.append({"task": t, "host": host, "path": path, "added": [l for s, l in body if s == "+"],
                    "removed": [l for s, l in body if s == "-"]})
    return res


DIFF_S1 = [("Каталог існує", "web1", None, []),
           ("Конфігурація з шаблону", "web1", "nodes/web1/etc/app.conf",
            [(" ", "# керується Ansible"), ("-", "workers 2;"), ("+", "workers 8;"), (" ", "listen 8080;")]),
           ("Конфігурація з шаблону", "web2", "nodes/web2/etc/app.conf",
            [(" ", "# керується Ansible"), ("-", "workers 2;"), ("+", "workers 8;"), (" ", "listen 8080;")])]
DIFF_S2 = [("Рядок ліміту", "db1", "nodes/db1/etc/limits.txt",
            [("+", "max_clients = 100")]),
           ("Конфігурація з шаблону", "db1", "nodes/db1/etc/pg.conf",
            [("-", "port = 5432"), ("-", "max_connections = 50"), ("+", "port = 5433"), ("+", "max_connections = 80"),
             (" ", "shared_buffers = 128MB")])]
DIFF_S3 = [("Каталог існує", "app1", None, []), ("Каталог існує", "app2", None, []),
           ("Список користувачів", "app1", "nodes/app1/etc/users.txt",
            [("-", "admin"), (" ", "teacher"), ("+", "student"), ("+", "guest")]),
           ("Список користувачів", "app2", "nodes/app2/etc/users.txt", [("-", "guest")]),
           ("Конфігурація з шаблону", "app2", "nodes/app2/etc/app.conf", [("+", "debug = true")])]


def c8_blocks(ds, res):
    assert res == expected_diff(ds["extra"]), f"{res}"


def c8_keys(ds, res):
    for r in res:
        assert set(r) == {"task", "host", "path", "added", "removed"}
        assert isinstance(r["added"], list) and isinstance(r["removed"], list)
    assert [(r["task"], r["host"]) for r in res] == [(t, h) for t, h, p, _ in ds["extra"] if p is not None]


def c8_nodiff(ds, res, mod):
    fn = getattr(mod, FUNC[8])
    assert fn("PLAY [x] ****\n\nTASK [y] ****\nok: [web1]\n\nPLAY RECAP ****\nweb1 : ok=1 changed=0 unreachable=0 failed=0\n") == []
    assert fn("") == []


def c8_real(ds, res, sbx, mod):
    need_ansible()
    fn = getattr(mod, FUNC[8])
    sbx.write("templates/app.conf.j2", "workers {{ workers }};\nlisten 8080;\n")
    play = yaml.safe_dump([{"name": "Перевірка", "hosts": "web", "gather_facts": False, "tasks": [
        {"name": "Каталог", "ansible.builtin.file": {"path": f"{R}/etc", "state": "directory"}},
        {"name": "Конфіг", "ansible.builtin.template": {"src": "app.conf.j2", "dest": f"{R}/etc/app.conf"}},
        {"name": "Рядок", "ansible.builtin.lineinfile": {"path": f"{R}/etc/x.txt", "line": "a=1", "create": True}}]}],
        allow_unicode=True, sort_keys=False)
    sbx.write("d.yml", play)
    rc, o = sbx.play("d.yml", "-e", "workers=2")
    assert rc == 0, o[-500:]
    sbx.write("d.yml", play.replace("a=1", "a=2"))
    rc, o = sbx.play("d.yml", "-e", "workers=8", "--check", "--diff")
    assert rc == 0, o[-500:]
    res = fn(o)
    assert [(r["task"], r["host"]) for r in res] == [("Конфіг", "web1"), ("Рядок", "web1"), ("Конфіг", "web2"),
                                                       ("Рядок", "web2")] or \
           sorted((r["task"], r["host"]) for r in res) == sorted([("Конфіг", "web1"), ("Рядок", "web1"),
                                                                    ("Конфіг", "web2"), ("Рядок", "web2")]), res
    for r in res:
        if r["task"] == "Конфіг":
            assert r["removed"] == ["workers 2;"] and r["added"] == ["workers 8;"], r
            assert r["path"].endswith(f"nodes/{r['host']}/etc/app.conf")
        else:
            assert r["added"] == ["a=2"] and r["removed"] == [] and r["path"].endswith(f"nodes/{r['host']}/etc/x.txt"), r


CHECKS[8] = [("blocks", c8_blocks, False), ("keys", c8_keys, False), ("nodiff", c8_nodiff, False),
             ("real", c8_real, True)]
DATA[8] = [dict(tag="S1", set=1, args=(diff_output(DIFF_S1),), extra=DIFF_S1),
           dict(tag="S2", set=2, args=(diff_output(DIFF_S2),), extra=DIFF_S2),
           dict(tag="S3", set=3, args=(diff_output(DIFF_S3),), extra=DIFF_S3)]


# ---------------------------------------------------------------------------------------------
# Задача 9. Vault
# ---------------------------------------------------------------------------------------------
VAULT_S1 = ({"db_host": "10.20.1.31", "db_port": 5432, "app_env": "prod"},
            {"db_password": "Kz9#pl:an", "api_token": "tok-3f9a-77"})
VAULT_S2 = ({"smtp_host": "mail.college.example", "smtp_port": 587},
            {"smtp_pass": "no", "pin_code": "007", "note_key": "x: y # z", "tilde": "~"})
VAULT_S3 = ({"redis_port": 6379, "tls": True},
            {"redis_secret": 424242, "backup_keys": ["alpha-1", "beta 2"], "admin_enabled": False})


def _load_pair(res):
    assert isinstance(res, tuple) and len(res) == 2 and all(isinstance(x, str) for x in res), \
        "функція має повернути кортеж із двох рядків (vars.yml, vault.yml)"
    try:
        return yaml.safe_load(res[0]), yaml.safe_load(res[1])
    except yaml.YAMLError as e:
        raise AssertionError(f"файл не є коректним YAML: {e}")


def c9_vault(ds, res):
    pub, sec = ds["args"]
    _, v = _load_pair(res)
    assert v == {"vault_" + k: val for k, val in sec.items()}, f"vault.yml: {v}"
    for k, val in sec.items():
        assert type(v["vault_" + k]) is type(val), f"тип значення {k} змінився"


def c9_vars(ds, res):
    pub, sec = ds["args"]
    o, _ = _load_pair(res)
    want = dict(pub)
    want.update({k: "{{ vault_" + k + " }}" for k in sec})
    assert o == want, f"vars.yml: {o}"


def c9_leak(ds, res):
    pub, sec = ds["args"]
    for val in sec.values():
        for piece in (val if isinstance(val, list) else [val]):
            if isinstance(piece, str) and len(piece) >= 4:
                assert piece not in res[0], f"секрет «{piece}» потрапив до відкритого файла vars.yml"
    assert res[0].startswith("---") and res[1].startswith("---"), "файли YAML починаються з ---"
    assert "ansible-vault" in res[1], "коментар у vault.yml має нагадувати про шифрування"


def c9_real(ds, res, sbx):
    need_ansible()
    pub, sec = ds["args"]
    sbx.write("group_vars/all/vars.yml", res[0])
    sbx.write("group_vars/all/vault.yml", res[1])
    sbx.write("pw.txt", "тестовий-пароль\n")
    rc, o = sbx.run(["ansible-vault", "encrypt", "--vault-password-file", "pw.txt", "group_vars/all/vault.yml"])
    assert rc == 0, o
    assert sbx.read("group_vars/all/vault.yml").startswith("$ANSIBLE_VAULT;1.1;AES256")
    names = list(pub) + list(sec)
    expr = "{{ {" + ", ".join(f"'{n}': {n}" for n in names) + "} | to_json }}"
    sbx.write("v.yml", yaml.safe_dump([{"name": "Читання", "hosts": "node1", "gather_facts": False, "tasks": [
        {"name": "Каталог вузла", "ansible.builtin.file": {"path": R, "state": "directory"}},
        {"name": "Запис результату", "ansible.builtin.copy": {"content": expr, "dest": f"{R}/resolved.json"}}]}],
        allow_unicode=True, sort_keys=False))
    rc, o = sbx.play("v.yml", "--vault-password-file", "pw.txt")
    assert rc == 0, o[-600:]
    got = json.loads((sbx.dir / "nodes/node1/resolved.json").read_text(encoding="utf-8"))
    want = dict(pub)
    want.update(sec)
    assert got == want, f"{got} != {want}"
    rc, o = sbx.play("v.yml")
    assert rc != 0, "без пароля сховища плейбук не повинен читати секрети"


CHECKS[9] = [("vault", c9_vault, False), ("vars", c9_vars, False), ("leak", c9_leak, False), ("real", c9_real, True)]
DATA[9] = [dict(tag="S1", set=1, args=VAULT_S1), dict(tag="S2", set=2, args=VAULT_S2),
           dict(tag="S3", set=3, args=VAULT_S3)]


# ---------------------------------------------------------------------------------------------
# Задача 10. Безпечне оновлення
# ---------------------------------------------------------------------------------------------
SAFE_S1 = dict(group="web", serial=1, max_fail=0, template="app.conf.j2", dest=f"{R}/etc/app/app.conf",
               validate="grep -q '^port' %s", handler="Перезапустити застосунок", log=f"{R}/etc/app/restart.log",
               required=["port"])
SAFE_S2 = dict(group="web", serial=2, max_fail=50, template="site.conf.j2", dest=f"{R}/etc/site/site.conf",
               validate="grep -q 'port' %s", handler="Перечитати сайт", log=f"{R}/etc/site/reload.log",
               required=["port", "workers"])
SAFE_S3 = dict(group="web", serial="50%", max_fail=20, template="gw.conf.j2", dest=f"{R}/etc/gateway/gw.conf",
               validate="grep -q '^port' %s", handler="Перезапустити шлюз", log=f"{R}/etc/gateway/gw.log",
               required=["port", "upstream"])


def c10_play(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    assert p["hosts"] == cfg["group"] and p.get("serial") == cfg["serial"], f"serial: {p.get('serial')}"
    assert p.get("max_fail_percentage") == cfg["max_fail"]
    assert p.get("name")
    assert not truthy(p.get("become")), "тут права адміністратора не потрібні"


def c10_assert(ds, text):
    cfg = ds["args"][0]
    t = only_play(text)["tasks"]
    assert module_of(t[0]) == "ansible.builtin.assert", "перевірка змінних має бути першим завданням"
    that = mod_params(t[0])["that"]
    that = [that] if isinstance(that, str) else that
    assert sorted(x.strip() for x in that) == sorted(f"{v} is defined" for v in cfg["required"]), that
    assert mod_params(t[0]).get("fail_msg")


def c10_template(ds, text):
    cfg = ds["args"][0]
    t = only_play(text)["tasks"]
    tp = find_tasks(t, "template")
    assert len(tp) == 1
    prm = mod_params(tp[0])
    assert prm["src"] == cfg["template"] and prm["dest"] == cfg["dest"]
    assert truthy(prm.get("backup")), "потрібна резервна копія: backup: true"
    assert prm.get("validate") == cfg["validate"]
    assert tp[0].get("notify") in (cfg["handler"], [cfg["handler"]])
    d = find_tasks(t, "file")
    assert d and mod_params(d[0])["state"] == "directory" and mod_params(d[0])["path"] == cfg["dest"].rsplit("/", 1)[0]
    order = [module_of(x) for x in t]
    assert order.index("ansible.builtin.assert") < order.index("ansible.builtin.file") < order.index(
        "ansible.builtin.template")


def c10_handler(ds, text):
    cfg = ds["args"][0]
    p = only_play(text)
    hs = p.get("handlers") or []
    assert len(hs) == 1 and hs[0]["name"] == cfg["handler"]
    body = str(mod_params(hs[0]))
    assert cfg["log"] in body, "обробник має дописувати рядок до журналу з cfg['log']"
    assert hs[0].get("changed_when") is True, "потрібен changed_when: true"


def c10_real(ds, text, sbx):
    need_ansible()
    cfg = ds["args"][0]
    values = {"port": "8080", "workers": "2", "upstream": "app1"}
    sbx.write(f"templates/{cfg['template']}", "port {{ port }};\nworkers {{ workers | default(2) }};\n")
    sbx.write("s.yml", text)

    def evars(**over):
        vals = dict(values, **over)
        return sum((["-e", f"{v}={vals[v]}"] for v in cfg["required"] if vals.get(v) is not None), [])

    def dest(h):
        return sbx.dir / "nodes" / h / cfg["dest"].replace(R + "/", "")

    def lines(h):
        p = sbx.dir / "nodes" / h / cfg["log"].replace(R + "/", "")
        return p.read_text(encoding="utf-8").count("\n") if p.exists() else 0

    rc, o = sbx.play("s.yml", *evars())
    assert rc == 0, "перший запуск:\n" + o[-700:]
    for h in ("web1", "web2"):
        assert dest(h).read_text(encoding="utf-8").startswith("port 8080;")
        assert lines(h) == 1
    rc, o = sbx.play("s.yml", *evars())
    assert rc == 0 and total_changed(o) == 0, "другий запуск має давати changed=0:\n" + o[-600:]
    assert lines("web1") == 1, "обробник не мав спрацювати"
    rc, o = sbx.play("s.yml", *evars(port="9090"), "--check", "--diff")
    assert rc == 0 and "9090" in o, o[-500:]
    assert dest("web1").read_text(encoding="utf-8").startswith("port 8080;"), "--check не повинен змінювати файл"
    assert lines("web1") == 1
    rc, o = sbx.play("s.yml", *evars(port="9090"))
    assert rc == 0, o[-500:]
    assert dest("web1").read_text(encoding="utf-8").startswith("port 9090;")
    assert lines("web1") == 2
    first = dest("web1")
    backups = [p for p in first.parent.iterdir() if p.name.startswith(first.name + ".")]
    assert backups, "після зміни має з'явитися резервна копія файла"
    keep = first.read_text(encoding="utf-8")
    rc, o = sbx.play("s.yml", *evars(port=None))
    assert rc != 0, "без обов'язкової змінної плейбук має зупинитися"
    assert first.read_text(encoding="utf-8") == keep
    sbx.write(f"templates/{cfg['template']}", "workers {{ workers | default(2) }};\n")
    rc, o = sbx.play("s.yml", *evars(port="7070"))
    assert rc != 0, "validate має зупинити розгортання хибного файла"
    assert first.read_text(encoding="utf-8") == keep, "хибний файл не повинен замінити робочий"


CHECKS[10] = [("play", c10_play, False), ("assert", c10_assert, False), ("template", c10_template, False),
              ("handler", c10_handler, False), ("real", c10_real, True)]
DATA[10] = [dict(tag="S1", set=1, args=(SAFE_S1,)), dict(tag="S2", set=2, args=(SAFE_S2,)),
            dict(tag="S3", set=3, args=(SAFE_S3,))]


# ---------------------------------------------------------------------------------------------
# Виконання перевірки
# ---------------------------------------------------------------------------------------------
def case_params(n, extra=()):
    """Параметри для pytest.mark.parametrize: (n, набір даних, ім'я перевірки), ідентифікатор T<n>-<тег>-<перевірка>."""
    out = []
    for ds in list(DATA[n]) + list(extra):
        if ds.get("error"):
            out.append(pytest.param(n, ds, "error", id=f"T{n:02d}-{ds['tag']}-error"))
            continue
        for name, _, _ in CHECKS[n]:
            out.append(pytest.param(n, ds, name, id=f"T{n:02d}-{ds['tag']}-{name}"))
    return out


def run_check(module, n, ds, name, tmp_path):
    """Викликає функцію студента на наборі ds і виконує перевірку name."""
    fn = getattr(module, FUNC[n])
    args = copy.deepcopy(ds["args"])
    if ds.get("error"):
        with pytest.raises(ValueError) as info:
            fn(*copy.deepcopy(args))
        assert str(info.value) == ds["error"], f"текст помилки: «{info.value}», очікується «{ds['error']}»"
        return
    needs = {c[0]: c for c in CHECKS[n]}[name]
    if needs[2]:
        need_ansible()
    result = fn(*copy.deepcopy(args))
    assert args == ds["args"] or json.dumps(args, default=str, sort_keys=True) == json.dumps(
        ds["args"], default=str, sort_keys=True), "функція не повинна змінювати вхідні дані"
    check = needs[1]
    argc = check.__code__.co_argcount
    names = check.__code__.co_varnames[:argc]
    kwargs = {}
    if "sbx" in names:
        kwargs["sbx"] = Sandbox(tmp_path / "lab") if ansible_bin() else None
    if "mod" in names:
        kwargs["mod"] = module
    check(ds, result, **kwargs)
