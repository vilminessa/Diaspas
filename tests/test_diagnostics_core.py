"""Диагностическое ядро: пробы, вердикты A/B, сводка перебора, состояние службы.

Раньше эти модули проверялись только живыми прогонами, а вся их логика
чистая: вердикт A/B и разбор ответа службы не требуют ни сети, ни прав.
"""

import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import abtest, autoselect, probes, service  # noqa: E402


# -- пробы -------------------------------------------------------------------

class FakeSocket:
    def __init__(self, answer=b"HTTP/1.1 200 OK\r\n", exc=None):
        self.answer = answer
        self.exc = exc
        self.sent = b""
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if self.exc:
            raise self.exc
        return self.answer

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, sock):
        self.sock = sock

    def wrap_socket(self, raw, server_hostname=None):
        self.host = server_hostname
        return self.sock


def patch_network(monkeypatch, connect=None, handshake_exc=None, answer=b"HTTP/1.1 200 OK\r\n",
                  recv_exc=None):
    sock = FakeSocket(answer=answer, exc=recv_exc)

    def create_connection(addr, timeout=None):
        if connect:
            return connect(addr, timeout)
        return sock

    monkeypatch.setattr(probes.socket, "create_connection", create_connection)

    class Ctx(FakeContext):
        def wrap_socket(self, raw, server_hostname=None):
            if handshake_exc:
                raise handshake_exc
            return super().wrap_socket(raw, server_hostname)

    monkeypatch.setattr(probes.ssl, "SSLContext", lambda proto: Ctx(sock))
    return sock


def test_probe_url_ok(monkeypatch):
    sock = patch_network(monkeypatch)
    rep = probes.probe_url("https://discord.com/api/gateway", timeout=2)
    assert rep["ok"] is True and rep["why"] == ""
    assert b"GET /api/gateway HTTP/1.1" in sock.sent
    assert b"Host: discord.com" in sock.sent
    assert sock.closed, "соединение должно закрываться и на успехе"


def test_probe_url_query_is_kept(monkeypatch):
    sock = patch_network(monkeypatch)
    probes.probe_url("https://redirector.googlevideo.com/videoplayback?id=7", timeout=2)
    assert b"GET /videoplayback?id=7 HTTP/1.1" in sock.sent


def test_probe_url_connect_timeout(monkeypatch):
    def slow(addr, timeout=None):
        raise TimeoutError()
    patch_network(monkeypatch, connect=slow)
    assert probes.probe_url("https://x.example/", timeout=1)["why"] == "timeout"


def test_probe_url_connect_refused(monkeypatch):
    def refused(addr, timeout=None):
        raise OSError("refused")
    patch_network(monkeypatch, connect=refused)
    rep = probes.probe_url("https://x.example/")
    assert rep["ok"] is False and rep["why"] == "conn"


def test_probe_url_tls_reset(monkeypatch):
    """tls - рукопожатие сброшено: именно это делает DPI с подменой трафика."""
    patch_network(monkeypatch, handshake_exc=ssl.SSLError("reset"))
    rep = probes.probe_url("https://x.example/")
    assert rep["ok"] is False and rep["why"] == "tls"


def test_probe_url_closed_without_answer(monkeypatch):
    patch_network(monkeypatch, answer=b"")
    assert probes.probe_url("https://x.example/")["why"] == "empty"


def test_probe_url_not_http(monkeypatch):
    patch_network(monkeypatch, answer=b"\x16\x03\x01 junk")
    assert probes.probe_url("https://x.example/")["why"] == "http"


def test_probe_url_recv_timeout(monkeypatch):
    patch_network(monkeypatch, recv_exc=TimeoutError())
    assert probes.probe_url("https://x.example/")["why"] == "timeout"


def test_probe_url_needs_host(monkeypatch):
    patch_network(monkeypatch)
    assert probes.probe_url("https:///only-path")["why"] == "conn"


def test_probe_group_states_and_order(monkeypatch):
    results = {
        "https://discord.com/": {"ok": True, "ms": 12, "why": ""},
        "https://discord.com/api/gateway": {"ok": False, "ms": 5, "why": "tls"},
        "https://cdn.discordapp.com/": {"ok": True, "ms": 20, "why": ""},
    }
    monkeypatch.setattr(probes, "probe_url", lambda url, timeout=6.0: results.get(
        url, {"ok": False, "ms": 0, "why": "conn"}))
    rep = probes.probe_group("discord", timeout=1)
    assert rep["state"] == "partial" and rep["count"] == 2 and rep["total"] == 3
    # порядок целей сохраняется: иначе строка журнала меняла вид при каждом замере
    assert list(rep["targets"]) == ["web", "api", "cdn"]
    assert rep["targets"]["api"]["why"] == "tls"


def test_probe_all_none_state(monkeypatch):
    monkeypatch.setattr(probes, "probe_url",
                        lambda url, timeout=6.0: {"ok": False, "ms": 0, "why": "conn"})
    rep = probes.probe_all(timeout=1)
    assert rep["state"] == "none" and rep["count"] == 0 and rep["total"] > 0
    line = probes.probe_line(rep)
    assert line.startswith("probe state=none 0/") and "conn" in line


def test_probe_quick_uses_base_groups_only(monkeypatch):
    seen = []
    monkeypatch.setattr(probes, "probe_group",
                        lambda g, timeout=6.0: seen.append(g) or {
                            "state": "full", "ok": True, "count": 2, "total": 2,
                            "targets": {}, "group": g})
    rep = probes.probe_quick(timeout=1)
    assert set(seen) == set(probes.VERIFY_GROUPS)
    assert rep["state"] == "full" and rep["total"] == 2 * len(probes.VERIFY_GROUPS)


def test_failed_targets_lists_group_and_name(monkeypatch):
    monkeypatch.setattr(probes, "probe_url", lambda url, timeout=6.0: {
        "ok": "discord.com" not in url and "ubisoft" not in url,
        "ms": 1, "why": ""})
    rep = probes.probe_all(timeout=1)
    failed = probes.failed_targets(rep)
    assert "discord.web" in failed and "discord.api" in failed, failed
    assert not any(f.startswith("youtube") for f in failed), failed


# -- вердикты A/B ------------------------------------------------------------

def rep(ok: bool, count=1, total=1) -> dict:
    return {"ok": ok, "count": count, "total": total, "state": "full" if ok else "none"}


@pytest.mark.parametrize("with_ok,without_ok,verdict", [
    (True, False, "zapret_needed"),      # блокирует DPI, стратегия справляется
    (False, True, "zapret_breaks"),      # ломает сам обход - домен в исключения
    (True, True, "works_always"),
    (False, False, "blocked_always"),
])
def test_ab_verdict_matrix(with_ok, without_ok, verdict):
    res = abtest._verdict("ubisoft", rep(with_ok), rep(without_ok))
    assert res["ok"] and res["verdict"] == verdict
    assert res["hint"]


def test_ab_verdict_without_second_measurement():
    """Служба изначально выключена: один замер - это «без обхода»."""
    assert abtest._verdict("ea", rep(True), None)["verdict"] == "works_without"
    assert abtest._verdict("ea", rep(False), None)["verdict"] == "blocked_anyway"


def test_ab_verdict_mentions_action(monkeypatch):
    """Вывод обязан подсказывать следующее действие, а не только диагноз."""
    res = abtest._verdict("ubisoft", rep(False), rep(True))
    assert "list-exclude-user.txt" in res["hint"]
    res2 = abtest._verdict("ubisoft", rep(False), None)
    assert "автоподбор" in res2["hint"] or "list-general-user.txt" in res2["hint"]


# -- сводка автоподбора ------------------------------------------------------

def test_summarize_marks_groups_and_best(tmp_path):
    (tmp_path / "general (ALT7).bat").write_text("x")
    (tmp_path / "general (ALT11).bat").write_text("x")
    result = {
        "best": "general (ALT7).bat",
        "results": [
            {"name": "general (ALT7).bat", "state": "full", "count": 3, "total": 3,
             "targets": [{"group": "discord", "ok": True},
                         {"group": "youtube", "ok": True},
                         {"group": "ea", "ok": True}]},
            {"name": "general (ALT11).bat", "state": "partial", "count": 2, "total": 3,
             "targets": [{"group": "discord", "ok": True},
                         {"group": "ea", "ok": False},
                         {"group": "ea", "ok": True}]},
        ],
    }
    rows = autoselect.summarize(result, tmp_path)
    assert rows[0]["best"] is True and rows[1]["best"] is False
    assert rows[0]["label"] == "ALT7", "короткая метка для GUI"
    assert rows[0]["groups"] == {"discord": "+", "youtube": "+", "ea": "+"}
    # один сбой гасит всю группу: «+» рядом с «-» читаются как шум
    assert rows[1]["groups"]["ea"] == "-"


def test_summarize_empty_result():
    assert autoselect.summarize({}, None) == []


# -- состояние службы --------------------------------------------------------

class FakeRegKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_LOCAL_MACHINE = 1

    def __init__(self, values, raise_open=False):
        self.values = values
        self.raise_open = raise_open

    def OpenKey(self, root, path):
        if self.raise_open:
            raise OSError("нет ключа")
        return FakeRegKey(self.values)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError("нет значения")
        return (key.values[name], 1)


def patch_service(monkeypatch, sc_text: str, returncode: int = 0,
                  values=None, raise_open: bool = False, pid: int = 0):
    class Completed:
        def __init__(self):
            self.stdout = sc_text.encode("cp1251")
            self.returncode = returncode

    def run(cmd, **kw):
        return Completed()

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr(service, "_winws_pid", lambda: pid)
    module = FakeWinreg(values or {}, raise_open)
    monkeypatch.setitem(sys.modules, "winreg", module)


def test_state_reads_running_exit_code_and_label(monkeypatch):
    patch_service(monkeypatch,
                  "SERVICE_NAME: zapret\n"
                  "        STATE               : 4  RUNNING\n"
                  "        WIN32_EXIT_CODE     : 0  (0x0)\n",
                  values={"zapret-discord-youtube": "general (ALT13)",
                          "Start": 2, "ImagePath": '"C:\\x\\winws.exe" --new'},
                  pid=4242)
    st = service.state()
    assert st.installed and st.running
    assert st.exit_code == 0
    assert st.strategy == "general (ALT13)"
    assert st.start_type == "Automatic"
    assert st.pid == 4242


def test_state_reads_stopped_with_exit_code(monkeypatch):
    patch_service(monkeypatch,
                  "SERVICE_NAME: zapret\n"
                  "        STATE               : 1  STOPPED\n"
                  "        WIN32_EXIT_CODE     : 1067  (0x42b)\n",
                  values={"Start": 3})
    st = service.state()
    assert st.installed and not st.running
    assert st.exit_code == 1067, "код выхода нужен, чтобы объяснить 1067"
    assert st.start_type == "Manual"


def test_state_without_service_is_not_installed(monkeypatch):
    patch_service(monkeypatch, "[SC] EnumQueryServicesStatus:OpenService FAILED 1060",
                  returncode=2, raise_open=True)
    st = service.state()
    assert st.installed is False and st.running is False
    assert st.strategy == "" and st.start_type == ""


def test_state_survives_russian_sc_output(monkeypatch):
    """sc query на русской Windows пишет состояния в cp1251 - декодирование обязательно."""
    patch_service(monkeypatch,
                  "СЛУЖБА_NAME: zapret\n        СОСТОЯНИЕ  : 4  RUNNING\n",
                  values={"zapret-discord-youtube": "general (ALT11)"})
    st = service.state()
    assert st.running is True
    assert st.strategy == "general (ALT11)"
