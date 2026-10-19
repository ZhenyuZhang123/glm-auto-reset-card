#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
glm-auto-reset-card — GLM Coding Plan 额度守护工具。

当 5 小时（可选：每周）额度剩余百分比低于阈值（默认 5%）时，自动使用
最早过期的对应类型重置卡恢复额度。

默认 dry-run：只报告将要做的事，不消耗任何卡。真实消耗需要同时满足：
  1) 命令行加 --execute
  2) 环境变量 GLM_AUTO_RESET_CONFIRM=YES

免责声明：本工具与智谱 / Z.ai 无关，调用的是其网页端使用的非公开接口，
接口随时可能变更；消耗重置卡不可撤销。详见 DISCLAIMER.md。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_BASE_URL = "https://open.bigmodel.cn"
ZAI_BASE_URL = "https://api.z.ai"
DEFAULT_THRESHOLD = 5.0          # 剩余百分比 <= 该值时触发用卡
DEFAULT_COOLDOWN_MIN = 10        # 成功用卡后的冷却期，防止异常情况下连环烧卡
HTTP_TIMEOUT = 30
LOCK_STALE_SECONDS = 15 * 60     # 锁文件超过该时长视为残留，可覆盖
CONFIRM_ENV = "GLM_AUTO_RESET_CONFIRM"
RECORD_ID_RE = re.compile(r"^[0-9]+$")

EXIT_OK = 0            # 正常：未触发 / 已成功用卡 / 无卡可用
EXIT_ERROR = 1         # 配置或 API 错误
EXIT_WOULD_BURN = 2    # dry-run 下本应烧卡（便于定时任务据此告警）
EXIT_AMBIGUOUS = 4     # 用卡请求结果未知，需运行 resolve 确认
EXIT_LOCKED = 5        # 已有实例在运行

POOL_FIVE_HOUR = "five_hour"
POOL_WEEK = "week"
POOL_TO_CARD_KEY = {POOL_FIVE_HOUR: "fiveHourResets", POOL_WEEK: "weekResets"}
POOL_TO_RESET_TYPE = {POOL_FIVE_HOUR: "FIVE_HOUR", POOL_WEEK: "WEEK"}


class ApiError(RuntimeError):
    pass


class ConfigError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fmt_ms(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


# ---------------------------------------------------------------- API 客户端

class Client:
    """bigmodel.cn / z.ai 网页端同源接口。鉴权用 API Key（Bearer）。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "glm-auto-reset-card/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ApiError(f"{method} {path} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # 网络层错误。对 GET 来说可以重试；对 POST 由调用方按"结果未知"处理。
            raise ConnectionError(f"{method} {path} 网络错误: {e}")
        if payload.get("code") != 200 or payload.get("success") is not True:
            raise ApiError(f"{method} {path} 业务失败: {payload.get('msg') or payload}")
        return payload.get("data")

    def get_limits(self) -> Any:
        return self._request("GET", "/api/monitor/usage/quota/limit")

    def list_cards(self) -> Any:
        return self._request("GET", "/api/biz/customer-package-reset/list?targetType=PERSONAL")

    def use_card(self, reset_type: str, record_id: int, request_id: str) -> Any:
        body = {
            "targetType": "PERSONAL",
            "resetType": reset_type,
            "recordId": record_id,
            "requestId": request_id,
        }
        return self._request("POST", "/api/biz/customer-package-reset/use", body)


# ------------------------------------------------------------ 额度 / 卡解析

def token_windows(limits_data: Any) -> list[dict]:
    """
    从 quota/limit 响应中取出 token 类额度窗口，按 nextResetTime 升序排列：
    第 0 个是 5 小时窗口，第 1 个是每周窗口（可能不存在）。

    注意：接口的 percentage 按"已用百分比"理解（与 GLMeter / glm-reset-card
    一致），剩余 = 100 - 已用。首次使用请先运行 check 与端内显示核对。
    """
    entries = [l for l in (limits_data or {}).get("limits", []) or []
               if l.get("type") == "TOKENS_LIMIT"]
    entries.sort(key=lambda l: l.get("nextResetTime") or 0)
    names = [POOL_FIVE_HOUR, POOL_WEEK]
    out = []
    for i, e in enumerate(entries[:2]):
        used = float(e.get("percentage") or 0)
        out.append({
            "pool": names[i],
            "used_pct": used,
            "remaining_pct": max(0.0, 100.0 - used),
            "next_reset_ms": e.get("nextResetTime"),
        })
    return out


def pick_card(cards_data: Any, pool: str) -> Optional[dict]:
    """选出指定池中最早过期的可用卡（与官方"先用过期最早"规则一致）。"""
    cards = [c for c in (cards_data or {}).get(POOL_TO_CARD_KEY[pool], []) or []
             if c.get("available")]
    cards.sort(key=lambda c: c.get("expireTime") or 0)
    return cards[0] if cards else None


# ---------------------------------------------------------------- 状态与锁

class State:
    """持久化用卡记录与"结果未知"标记，目录默认 ~/.glm-auto-reset-card。"""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.lock_path = state_dir / "lock"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"burns": [], "ambiguous": None}

    def save(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def acquire_lock(self) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.lock_path.exists():
                age = time.time() - self.lock_path.stat().st_mtime
                if age < LOCK_STALE_SECONDS:
                    return False
                log(f"发现残留锁（{age/60:.0f} 分钟前），视为崩溃残留，覆盖。")
            self.lock_path.write_text(str(os.getpid()), encoding="utf-8")
            return True
        except OSError as e:
            raise ConfigError(f"无法创建锁文件 {self.lock_path}: {e}")

    def release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ 子命令

def resolve_api_key(args) -> str:
    key = args.api_key or os.environ.get("GLM_API_KEY")
    if not key:
        raise ConfigError(
            "缺少 API Key：请设置环境变量 GLM_API_KEY，或用 --api-key 传入。\n"
            "获取入口：https://open.bigmodel.cn 控制台（国际站：https://z.ai）。"
        )
    return key


def resolve_base_url(args) -> str:
    return (args.base_url or os.environ.get("GLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def build_client(args) -> Client:
    return Client(resolve_base_url(args), resolve_api_key(args),
                  timeout=args.timeout)


def cmd_check(args) -> int:
    """只读：显示额度窗口与重置卡库存，用于核对 percentage 语义。"""
    client = build_client(args)
    windows = token_windows(client.get_limits())
    if not windows:
        log("未在响应中找到 TOKENS_LIMIT 窗口（套餐可能不含按量额度）。")
        return EXIT_ERROR

    log("额度窗口（假设 percentage=已用百分比，剩余=100-已用；请与端内显示核对）：")
    for w in windows:
        log(f"  {w['pool']:<9} 已用 {w['used_pct']:5.1f}%  剩余 {w['remaining_pct']:5.1f}%"
            f"  下次自然重置 {fmt_ms(w['next_reset_ms'])}"
            f"{'   <-- 低于阈值将触发' if w['remaining_pct'] <= args.threshold else ''}")

    cards = client.list_cards()
    any_card = False
    for pool in (POOL_FIVE_HOUR, POOL_WEEK):
        for c in (cards or {}).get(POOL_TO_CARD_KEY[pool], []) or []:
            any_card = True
            status = "可用" if c.get("available") else "已用/失效"
            log(f"  重置卡[{pool}] recordId={c.get('recordId')} {status}"
                f"  过期 {fmt_ms(c.get('expireTime'))}")
    if not any_card:
        log("  当前没有重置卡（若刚领取，稍后再查）。")
    if windows and windows[0]["remaining_pct"] <= args.threshold:
        log(f"5 小时额度剩余已 <= {args.threshold}%。")
    return EXIT_OK


def cmd_run(args) -> int:
    """守护主流程：额度不足 -> 选最早过期卡 -> （双重确认后）消耗。"""
    state = State(Path(args.state_dir).expanduser())
    if not state.acquire_lock():
        log("已有实例在运行（锁文件存在），本次跳过。")
        return EXIT_LOCKED
    try:
        return _run_guard(args, state)
    finally:
        state.release_lock()


def _run_guard(args, state: State) -> int:
    client = build_client(args)
    state_data = state.load()

    if state_data.get("ambiguous"):
        amb = state_data["ambiguous"]
        log(f"存在结果未知的用卡请求（recordId={amb.get('recordId')}，"
            f"requestId={amb.get('requestId')}），已停止自动用卡。"
            f"请运行 resolve 子命令确认后才会继续。")
        return EXIT_AMBIGUOUS

    pools = [POOL_FIVE_HOUR, POOL_WEEK] if args.pool == "both" else [args.pool]
    windows = {w["pool"]: w for w in token_windows(client.get_limits())}

    to_burn = []
    for pool in pools:
        w = windows.get(pool)
        if w is None:
            log(f"额度窗口 [{pool}] 不存在（套餐可能没有每周窗口），跳过。")
            continue
        log(f"[{pool}] 已用 {w['used_pct']:.1f}%，剩余 {w['remaining_pct']:.1f}%"
            f"（触发线：剩余 <= {args.threshold}%）")
        if w["remaining_pct"] > args.threshold:
            continue
        # 冷却检查：刚用卡成功说明额度刚恢复，剩余不应立刻又过低，
        # 若出现多半是 percentage 语义不对或接口异常，宁可不烧。
        last = _last_burn(state_data, pool)
        if last and time.time() - last["ts"] < args.cooldown_minutes * 60:
            log(f"[{pool}] 距上次用卡成功不足 {args.cooldown_minutes} 分钟，"
                f"为安全起见本轮跳过（若反复出现请用 check 核对额度语义）。")
            continue
        to_burn.append(pool)

    if not to_burn:
        log("无需用卡。")
        return EXIT_OK

    cards = client.list_cards()
    plan = []
    for pool in to_burn:
        card = pick_card(cards, pool)
        if card is None:
            log(f"[{pool}] 额度已低于阈值，但没有可用重置卡，只能等自然重置。")
            continue
        plan.append((pool, card))

    if not plan:
        return EXIT_OK

    for pool, card in plan:
        log(f"[{pool}] 将使用最早过期卡：recordId={card.get('recordId')}，"
            f"过期 {fmt_ms(card.get('expireTime'))}")

    if not args.execute:
        for pool, card in plan:
            log(f"[dry-run] 不会真的消耗。若要真实消耗需：命令行加 --execute，"
                f"且环境变量 {CONFIRM_ENV}=YES。")
        return EXIT_WOULD_BURN

    if os.environ.get(CONFIRM_ENV) != "YES":
        log(f"已加 --execute，但环境变量 {CONFIRM_ENV} 未设为 YES，拒绝消耗。"
            f"这是防止误烧卡的双重确认。")
        return EXIT_ERROR

    for pool, card in plan:
        record_id = card.get("recordId")
        if not RECORD_ID_RE.match(str(record_id)):
            log(f"[{pool}] recordId 非法（{record_id!r}），跳过。")
            continue
        _burn(client, state, pool, int(record_id), args)
    return EXIT_OK


def _last_burn(state_data: dict, pool: str) -> Optional[dict]:
    burns = [b for b in state_data.get("burns", []) if b.get("pool") == pool]
    return burns[-1] if burns else None


def _burn(client: Client, state: State, pool: str, record_id: int, args) -> None:
    request_id = str(uuid.uuid4())
    reset_type = POOL_TO_RESET_TYPE[pool]
    state_data = state.load()
    try:
        client.use_card(reset_type, record_id, request_id)
    except ConnectionError as e:
        # 请求可能已发出但结果未知：绝不自动重试（可能烧掉第二张卡）。
        state_data["ambiguous"] = {
            "pool": pool, "recordId": record_id,
            "requestId": request_id, "ts": time.time(), "error": str(e),
        }
        state.save(state_data)
        log(f"[{pool}] 用卡请求网络异常且结果未知，已记录并停止后续自动用卡。"
            f"请运行 resolve 确认该卡是否已被消耗。")
        sys.exit(EXIT_AMBIGUOUS)
    except ApiError as e:
        log(f"[{pool}] 用卡失败（未消耗）：{e}")
        return
    state_data.setdefault("burns", []).append({
        "pool": pool, "resetType": reset_type, "recordId": record_id,
        "requestId": request_id, "ts": time.time(), "result": "ok",
    })
    state.save(state_data)
    log(f"[{pool}] 重置卡已消耗：recordId={record_id}，requestId={request_id}。"
        f"对应额度应已恢复 100%，可用 check 验证。")


def cmd_resolve(args) -> int:
    """确认"结果未知"的用卡请求到底有没有消耗卡。"""
    state = State(Path(args.state_dir).expanduser())
    state_data = state.load()
    amb = state_data.get("ambiguous")
    if not amb:
        log("没有待确认的未知结果请求。")
        return EXIT_OK
    client = build_client(args)
    cards = client.list_cards()
    still_there = False
    for c in (cards or {}).get(POOL_TO_CARD_KEY[amb["pool"]], []) or []:
        if str(c.get("recordId")) == str(amb["recordId"]) and c.get("available"):
            still_there = True
            break
    if still_there:
        state_data["ambiguous"] = None
        state.save(state_data)
        log(f"recordId={amb['recordId']} 仍可用，说明上次请求没有生效，已清除标记，"
            f"自动用卡恢复。如需用这张卡请手动操作或重新 run。")
    else:
        state_data.setdefault("burns", []).append({
            "pool": amb["pool"], "resetType": POOL_TO_RESET_TYPE[amb["pool"]],
            "recordId": amb["recordId"], "requestId": amb["requestId"],
            "ts": amb["ts"], "result": "consumed(resolved)",
        })
        state_data["ambiguous"] = None
        state.save(state_data)
        log(f"recordId={amb['recordId']} 已不在可用列表，确认被上次请求消耗，"
            f"已记入历史。自动用卡恢复。")
    return EXIT_OK


# ------------------------------------------------------------------ 自测试

def cmd_selftest(args) -> int:
    """内置 mock 服务器全流程自测，不访问任何真实接口。"""
    import tempfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    now = time.time() * 1000
    backend = {"used": 97.0, "cards": [
        {"available": True, "expireTime": now + 86400_000, "recordId": 111},
        {"available": True, "expireTime": now + 3600_000, "recordId": 100},
    ], "use_hang": False, "received_use": []}

    class Handler(BaseHTTPRequestHandler):
        def _json(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/monitor/usage/quota/limit"):
                self._json({"code": 200, "success": True, "data": {"limits": [
                    {"type": "TOKENS_LIMIT", "percentage": backend["used"],
                     "nextResetTime": now + 3600_000},
                    {"type": "TOKENS_LIMIT", "percentage": 40.0,
                     "nextResetTime": now + 5 * 86400_000},
                ]}})
            elif self.path.startswith("/api/biz/customer-package-reset/list"):
                self._json({"code": 200, "success": True, "data": {
                    "fiveHourResets": backend["cards"], "weekResets": []}})
            else:
                self._json({"code": 404, "success": False, "msg": "not found"}, 200)

        def do_POST(self):
            if self.path.startswith("/api/biz/customer-package-reset/use"):
                length = int(self.headers.get("Content-Length") or 0)
                backend["received_use"].append(json.loads(self.rfile.read(length)))
                if backend["use_hang"]:
                    time.sleep(5)  # 超过客户端超时，制造"结果未知"
                self._json({"code": 200, "success": True, "data": {"ok": True}})
            else:
                self._json({"code": 404, "success": False, "msg": "not found"}, 200)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    tmp = Path(tempfile.mkdtemp(prefix="glm-arc-test-"))
    results = []

    def make_args(**kw):
        defaults = dict(api_key="test-key", base_url=base, threshold=5.0,
                        pool=POOL_FIVE_HOUR, execute=False, timeout=1.0,
                        state_dir=str(tmp), cooldown_minutes=10)
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def run_case(name, expect_exit, **kw):
        state = State(tmp)
        if not state.acquire_lock():
            results.append((name, False, "lock failed")); return
        try:
            code = _run_guard(make_args(**kw), state)
        except SystemExit as e:  # _burn 在结果未知时用 sys.exit(4) 中止
            code = e.code
        finally:
            state.release_lock()
        results.append((name, code == expect_exit, f"exit={code} 期望={expect_exit}"))

    state = State(tmp)

    # 1. check：可读额度与卡（只在输出层面，断言不抛错即可）
    os.environ["GLM_API_KEY"] = "test-key"
    try:
        check_args = make_args()
        cmd_check(check_args)
        results.append(("check", True, "ok"))
    except Exception as e:
        results.append(("check", False, str(e)))

    # 2. 剩余 3% <= 5%：dry-run 应返回 EXIT_WOULD_BURN 且不发生 POST
    run_case("dry-run 触发但不烧卡", EXIT_WOULD_BURN)
    ok = len(backend["received_use"]) == 0
    results.append(("dry-run 未发 POST", ok, f"POST 数={len(backend['received_use'])}"))

    # 3. execute + 确认环境变量：烧最早过期的那张（recordId=100）
    os.environ[CONFIRM_ENV] = "YES"
    run_case("execute 烧最早过期卡", EXIT_OK, execute=True)
    uses = backend["received_use"]
    ok = len(uses) == 1 and uses[0]["recordId"] == 100 \
        and uses[0]["resetType"] == "FIVE_HOUR" and len(uses[0]["requestId"]) == 36
    results.append(("选中最早过期卡+幂等字段", ok,
                    f"uses={json.dumps(uses, ensure_ascii=False)}"))

    # 4. 冷却期：再次触发应跳过
    run_case("冷却期内不连烧", EXIT_OK, execute=True)
    ok = len(backend["received_use"]) == 1
    results.append(("冷却期未发第二次 POST", ok,
                    f"POST 数={len(backend['received_use'])}"))

    # 5. 额度充足：不触发
    backend["used"] = 50.0
    state.load(); sd = state.load(); sd["burns"] = []; state.save(sd)
    run_case("额度充足不触发", EXIT_OK, execute=True)
    results.append(("额度充足未发 POST", len(backend["received_use"]) == 1,
                    f"POST 数={len(backend['received_use'])}"))

    # 6. 无可用卡：不触发
    backend["used"] = 99.0
    backend["cards"] = []
    run_case("无卡时不触发", EXIT_OK, execute=True)

    # 7. 网络结果未知：记录 ambiguous 并返回 EXIT_AMBIGUOUS，不重试
    backend["cards"] = [{"available": True, "expireTime": now + 3600_000,
                         "recordId": 200}]
    backend["use_hang"] = True
    run_case("结果未知进入 ambiguous", EXIT_AMBIGUOUS, execute=True)
    sd = state.load()
    results.append(("ambiguous 已持久化", bool(sd.get("ambiguous")),
                    json.dumps(sd.get("ambiguous"), ensure_ascii=False)))

    # 8. resolve：卡已被消耗（mock 的卡列表里 200 仍标记可用？
    #    不会——hang 分支没有真正改状态，这里手动把卡标记为已用模拟真实消耗）
    backend["use_hang"] = False
    backend["cards"] = [{"available": False, "expireTime": now + 3600_000,
                         "recordId": 200}]
    code = cmd_resolve(make_args())
    results.append(("resolve 确认已消耗", code == EXIT_OK, f"exit={code}"))
    sd = state.load()
    ok = sd.get("ambiguous") is None and \
        any(b.get("result") == "consumed(resolved)" for b in sd.get("burns", []))
    results.append(("resolve 后状态恢复", ok, json.dumps(sd, ensure_ascii=False)[:200]))

    # 9. 双重确认：execute 但未设确认环境变量 -> 拒绝
    os.environ.pop(CONFIRM_ENV, None)
    sd = state.load(); sd["burns"] = []; sd["ambiguous"] = None; state.save(sd)
    backend["used"] = 97.0
    backend["cards"] = [{"available": True, "expireTime": now + 3600_000,
                         "recordId": 300}]
    n_before = len(backend["received_use"])
    run_case("缺确认环境变量时拒绝", EXIT_ERROR, execute=True)
    results.append(("未设确认时未发 POST", len(backend["received_use"]) == n_before,
                    f"POST 数={len(backend['received_use'])}"))

    server.shutdown()
    os.environ.pop("GLM_API_KEY", None)

    failed = 0
    print()
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + ("" if ok else f"  -- {detail}"))
    print(f"\n{len(results) - failed}/{len(results)} 通过。以上全部基于本地 mock，"
          f"未访问真实接口、未消耗任何卡。")
    return EXIT_ERROR if failed else EXIT_OK


# -------------------------------------------------------------------- 入口

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="glm-auto-reset-card",
        description="GLM Coding Plan 额度守护：剩余额度低于阈值时自动使用最早过期的重置卡。"
                    "默认 dry-run，真实消耗需 --execute + 环境变量 "
                    f"{CONFIRM_ENV}=YES 双重确认。",
    )
    p.add_argument("--api-key", help="智谱 API Key；默认读环境变量 GLM_API_KEY")
    p.add_argument("--base-url", help=f"默认 {DEFAULT_BASE_URL}，国际站用 {ZAI_BASE_URL}；"
                                     "默认读环境变量 GLM_BASE_URL")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"剩余百分比触发线，默认 {DEFAULT_THRESHOLD}（即剩不到 5%% 时用卡）")
    p.add_argument("--pool", choices=[POOL_FIVE_HOUR, POOL_WEEK, "both"],
                   default=POOL_FIVE_HOUR, help="守护哪个额度池，默认 5 小时")
    p.add_argument("--state-dir", default="~/.glm-auto-reset-card",
                   help="状态目录（锁/用卡历史），默认 ~/.glm-auto-reset-card")
    p.add_argument("--cooldown-minutes", type=float, default=DEFAULT_COOLDOWN_MIN,
                   help=f"成功用卡后的冷却分钟数，默认 {DEFAULT_COOLDOWN_MIN}")
    p.add_argument("--timeout", type=float, default=HTTP_TIMEOUT,
                   help="单次 HTTP 超时秒数，默认 30")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="只读：查看额度窗口与重置卡库存（用于核对语义）")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("run", help="执行一轮守护（默认 dry-run）")
    sp.add_argument("--execute", action="store_true",
                    help=f"允许真实消耗重置卡（还需环境变量 {CONFIRM_ENV}=YES）")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resolve", help="确认结果未知的用卡请求，恢复自动流程")
    sp.set_defaults(func=cmd_resolve)

    sub.add_parser("selftest", help="本地 mock 自测（不访问真实接口）").set_defaults(
        func=cmd_selftest)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        log(f"配置错误：{e}")
        return EXIT_ERROR
    except ApiError as e:
        log(f"接口调用失败：{e}")
        return EXIT_ERROR
    except ConnectionError as e:
        log(f"网络错误：{e}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
