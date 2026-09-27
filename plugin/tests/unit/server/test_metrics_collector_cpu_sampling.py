from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.server.monitoring import metrics as module


class _FakePsProcess:
    instances: list["_FakePsProcess"] = []

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.cpu_calls: list[object] = []
        _FakePsProcess.instances.append(self)

    def cpu_percent(self, interval: object = "unset") -> float:
        self.cpu_calls.append(interval)
        # 模拟 psutil：首次调用建立基线返回 0.0
        return 0.0 if len(self.cpu_calls) == 1 else 12.5

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=64 * 1024 * 1024)

    def memory_percent(self) -> float:
        return 1.5

    def num_threads(self) -> int:
        return 4


class _FakeMpProcess:
    def __init__(self, pid: int, alive: bool = True) -> None:
        self.pid = pid
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def _host(pid: int, alive: bool = True) -> SimpleNamespace:
    return SimpleNamespace(process=_FakeMpProcess(pid, alive), comm_manager=None)


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> type[_FakePsProcess]:
    _FakePsProcess.instances = []

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    fake = SimpleNamespace(
        Process=_FakePsProcess,
        NoSuchProcess=_NoSuchProcess,
        AccessDenied=_AccessDenied,
    )
    monkeypatch.setattr(module, "psutil", fake)
    monkeypatch.setattr(module, "PSUTIL_AVAILABLE", True)
    return _FakePsProcess


@pytest.mark.plugin_unit
def test_cpu_sampling_is_non_blocking_and_reuses_process(fake_psutil: type[_FakePsProcess]) -> None:
    collector = module.MetricsCollector()
    host = _host(1001)

    first = collector._collect_plugin_metrics_sync("demo", host)
    second = collector._collect_plugin_metrics_sync("demo", host)

    assert first is not None and second is not None
    assert len(fake_psutil.instances) == 1
    assert fake_psutil.instances[0].cpu_calls == [None, None]
    assert first.cpu_percent == 0.0
    assert second.cpu_percent == 12.5
    assert second.memory_mb == 64.0
    assert second.num_threads == 4


@pytest.mark.plugin_unit
def test_cpu_sampling_cache_resets_on_pid_change(fake_psutil: type[_FakePsProcess]) -> None:
    collector = module.MetricsCollector()

    collector._collect_plugin_metrics_sync("demo", _host(1001))
    collector._collect_plugin_metrics_sync("demo", _host(1001))
    restarted = collector._collect_plugin_metrics_sync("demo", _host(2002))

    assert [p.pid for p in fake_psutil.instances] == [1001, 2002]
    assert restarted is not None
    assert restarted.pid == 2002
    assert restarted.cpu_percent == 0.0
    assert collector._ps_processes["demo"][0] == 2002


@pytest.mark.plugin_unit
def test_cpu_sampling_cache_dropped_when_process_dead(fake_psutil: type[_FakePsProcess]) -> None:
    collector = module.MetricsCollector()

    collector._collect_plugin_metrics_sync("demo", _host(1001))
    assert "demo" in collector._ps_processes

    assert collector._collect_plugin_metrics_sync("demo", _host(1001, alive=False)) is None
    assert "demo" not in collector._ps_processes


@pytest.mark.plugin_unit
def test_cpu_sampling_cache_dropped_on_no_such_process(
    fake_psutil: type[_FakePsProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = module.MetricsCollector()
    collector._collect_plugin_metrics_sync("demo", _host(1001))

    def _gone(self: _FakePsProcess, interval: object = "unset") -> float:
        raise module.psutil.NoSuchProcess()

    monkeypatch.setattr(_FakePsProcess, "cpu_percent", _gone)

    assert collector._collect_plugin_metrics_sync("demo", _host(1001)) is None
    assert "demo" not in collector._ps_processes


@pytest.mark.plugin_unit
def test_prune_drops_plugins_no_longer_in_hosts(fake_psutil: type[_FakePsProcess]) -> None:
    collector = module.MetricsCollector()
    collector._collect_plugin_metrics_sync("keep", _host(1001))
    collector._collect_plugin_metrics_sync("gone", _host(1002))

    collector._prune_ps_processes({"keep": object()})

    assert set(collector._ps_processes) == {"keep"}
