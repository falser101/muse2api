"""账号集合选号、额度差和任务用量，不访问网络。"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app
from engine import MuseEngine
from store import Store


def _store(tmp):
    accounts = tmp / "accounts.json"
    tasks = tmp / "tasks.json"
    accounts.write_text("[]", encoding="utf-8")
    tasks.write_text("{}", encoding="utf-8")
    return Store(SimpleNamespace(accounts_file=str(accounts), tasks_file=str(tasks)))


def _account(aid, last_used, percent=None, exhausted=False):
    item = {
        "id": aid,
        "label": aid,
        "cookies": {"hatch_sess": "present"},
        "enabled": True,
        "ok": True,
        "last_used": last_used,
        "use_count": 0,
        "quota_exhausted": exhausted,
    }
    if percent is not None:
        item["quota"] = {"weekly_used_pct": percent}
    return item


def test_header_and_selection(tmp_path):
    assert app.parse_account_header(None) is None
    assert app.parse_account_header("  ") is None
    assert app.parse_account_header("low, high, low") == ["low", "high"]
    try:
        app.parse_account_header("bad id")
    except app.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("invalid account id should be rejected")

    st = _store(tmp_path)
    st.accounts = [
        _account("old", 0, 90),
        _account("low", 50, 10),
        _account("high", 10, 80),
        _account("spent", 1, 100, exhausted=True),
    ]
    # 不带集合时仍按最久未用选择，不看额度。
    assert st.pick_account(record_use=False)["id"] == "old"
    assert st.pick_account(allowed_ids=["low", "high", "spent"], record_use=False)["id"] == "low"
    assert st.pick_account(allowed_ids=["spent"], record_use=False) is None
    assert st.pick_account(allowed_ids=["missing"], record_use=False) is None

    pool = app.AccountPoolManager(st)
    first = pool.acquire(allowed_ids=["low", "high", "spent", "old"], timeout=1, record_use=False)
    second = pool.acquire(allowed_ids=["low", "high", "spent", "old"], timeout=1, record_use=False)
    assert first["id"] == "low"
    assert second["id"] == "high"
    try:
        pool.acquire(allowed_ids=["spent"], timeout=1, record_use=False)
    except app.MuseGenerationError as exc:
        assert "额度" in str(exc)
    else:
        raise AssertionError("exhausted account should not be leased")
    pool.release(first["id"])
    again = pool.acquire(allowed_ids=["low", "high"], timeout=1, record_use=False)
    assert again["id"] == "low"


def test_usage_delta_and_task_record(tmp_path):
    payload = "\n".join([
        '0:{"ok":true}',
        '1:{"success":true,"subscription":{"usageRowLabel":"Free",'
        '"statusSubtitle":"Weekly limit resets on Nov 20",'
        '"usage":{"percentUsed":28,"resetsAt":1795200008,"quotaStatus":"SUFFICIENT"},'
        '"topupBalance":40000000,"topupTotal":40000000,'
        '"topupRowLabel":"Additional tokens","topupRowValueLabel":"0% used (1B tokens left)"}}',
    ])
    parsed = MuseEngine.parse_subscription_payload(payload)
    assert parsed["found"] is True
    assert parsed["weekly_used_pct"] == 28
    assert parsed["extra_balance"] == 40000000
    assert parsed["extra_left"] == "0% used (1B tokens left)"
    assert MuseEngine.quota_exhausted(parsed) is False
    assert MuseEngine.quota_exhausted({"quota_status": "EXHAUSTED", "weekly_used_pct": 10}) is True
    assert MuseEngine.quota_exhausted({"weekly_used_pct": 100, "extra_balance": 0}) is True
    assert MuseEngine.quota_exhausted({"weekly_used_pct": 100, "extra_balance": 20}) is False

    before = {"weekly_used_pct": 28, "weekly_resets_at": 1795200008, "extra_balance": 40000000}
    after = {"weekly_used_pct": 29, "weekly_resets_at": 1795200008, "extra_balance": 39900000,
             "quota_status": "SUFFICIENT", "checked_at": 10}
    usage = MuseEngine.usage_delta("acc", before, after)
    assert usage["weekly_delta_pct"] == 1
    assert usage["extra_delta_tokens"] == 100000
    reset = dict(after, weekly_resets_at=1795200009)
    assert "weekly_delta_pct" not in MuseEngine.usage_delta("acc", before, reset)

    st = _store(tmp_path)
    task = st.create_task("image", "cat")
    saved = st.update_task(task["id"], status="completed", usage=usage)
    assert saved["usage"]["account"] == "acc"
    assert saved["usage"]["weekly_delta_pct"] == 1
    assert st.get_task(task["id"])["usage"]["extra_delta_tokens"] == 100000


def _use_store(st):
    previous = app.store
    app.store = st
    return previous


def _subscription(percent, extra):
    return {
        "found": True,
        "weekly_used_pct": percent,
        "weekly_resets_at": 1795200008,
        "extra_balance": extra,
        "quota_status": "SUFFICIENT",
    }


def test_meter_uses_explicit_before_not_stale_cache(tmp_path, monkeypatch):
    st = _store(tmp_path)
    acc = _account("acc", 0, 1)
    acc["quota"].update({"extra_balance": 1, "weekly_resets_at": 1795200008, "checked_at": 1})
    st.accounts = [acc]
    previous = _use_store(st)
    monkeypatch.setattr(app.engine, "read_subscription", lambda *args, **kwargs: _subscription(12, 80))
    try:
        usage = app._meter_account(st.get_account("acc"), {
            "weekly_used_pct": 11, "weekly_resets_at": 1795200008, "extra_balance": 90,
        })
        assert usage["weekly_delta_pct"] == 1
        assert usage["extra_delta_tokens"] == 10

        stale = st.get_account("acc")
        stale["quota"]["checked_at"] = 1
        st.update_account("acc", quota=stale["quota"])
        missed = app._meter_account(st.get_account("acc"))
        assert missed["weekly_used_pct_after"] == 12
        assert "weekly_delta_pct" not in missed
        assert "extra_delta_tokens" not in missed

        fresh = st.get_account("acc")
        fresh["quota"]["weekly_used_pct"] = 10
        fresh["quota"]["extra_balance"] = 100
        fresh["quota"]["checked_at"] = int(time.time())
        st.update_account("acc", quota=fresh["quota"])
        cached = app._meter_account(st.get_account("acc"))
        assert cached["weekly_delta_pct"] == 2
        assert cached["extra_delta_tokens"] == 20
    finally:
        app.store = previous


def test_quota_fallback_skips_browser(tmp_path, monkeypatch):
    st = _store(tmp_path)
    acc = _account("acc", 0, 8)
    st.accounts = [acc]
    previous = _use_store(st)

    def unavailable(*args, **kwargs):
        raise app.MuseGenerationError("down")

    def browser(*args, **kwargs):
        raise AssertionError("browser quota path should not run")

    monkeypatch.setattr(app.engine, "read_subscription", unavailable)
    monkeypatch.setattr(app.engine, "quota", browser)
    try:
        try:
            app._probe_quota("acc", "0")
        except app.MuseGenerationError as exc:
            assert "额度接口不可用" in str(exc)
        else:
            raise AssertionError("http failure should surface")
        assert st.get_account("acc")["quota"]["weekly_used_pct"] == 8
    finally:
        app.store = previous
