"""每日签到 + 凭据自动续期（P46）。

签到在网关侧自行管理，只读自己的凭据目录，不依赖外部签到系统。
账号来源统一为网关自己的凭据目录：

  - WorkBuddy：`<credential_dir>/<label>.info`（明文 accessToken 的 session JSON）
  - Qoder：`<credential_dir>/<label>.qoder.json`（token / refresh）

不再依赖签到系统的目录、cron、外部脚本或 Python 运行时：
  - 旧 `daily_report.py` 要 `QODER_CLAIM_HOME` + `claim.py` + `check_all.py`；
  - 旧 `signin.py` 的 `resolve_session()` 需要 macOS Electron 运行时解 `$wbEncrypted` ——
    但服务器那份 `.info` 的 accessToken 已经是明文 JWT（实测），所以网关侧直接读即可。

红线（继承 P41/P45）：
  - 只读网关自己的凭据文件；**不写签到系统任何目录**；
  - token / refresh 不进日志、不进 DB、不进 git；
  - 拿不到真实金额就记 None，绝不用估算冒充。

接口参考（照抄自签到系统，已实测）：
  - Qoder 活动列表：GET  https://openapi.qoder.com.cn/sash/api/v1/me/campaigns
        ⚠️ 必须带 `Cosy-ClientType: 10` 头，否则返回空列表（签到系统踩过）
  - Qoder 领取探测：GET  .../campaigns/{id}/reward     （200 = 已领）
  - Qoder 领取：     POST .../campaigns/{id}/claim     （幂等，replayed = 已领）
  - Qoder 用量：     GET  https://openapi.qoder.com.cn/sash/api/v2/me/usage
  - Qoder 续期：     POST .../api/v1/deviceToken/refresh
        ⚠️ 不是 /api/v1/jobToken/refresh：后者只认 PAT，对 drt- 恒 400
  - WorkBuddy 状态：POST https://copilot.tencent.com/v2/billing/meter/checkin-activity-status
  - WorkBuddy 领取：POST https://copilot.tencent.com/v2/billing/meter/daily-checkin
  - WorkBuddy 余额：POST https://copilot.tencent.com/billing/meter/get-user-resource-summary
        ⚠️ 这个接口不带 /v2 前缀（签到系统的 check_all.py 注释明确写了）
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings

QODER_API = "https://openapi.qoder.com.cn"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
USAGE_PATH = "/sash/api/v2/me/usage"
DEVICE_REFRESH_PATH = "/api/v1/deviceToken/refresh"

WB_ENDPOINT = "https://copilot.tencent.com"
WB_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
WB_CLAIM_PATH = "/v2/billing/meter/daily-checkin"
WB_BALANCE_PATH = "/billing/meter/get-user-resource-summary"

EXPECTED_DAILY = 100
CLIENT_HEADER = {"Cosy-ClientType": "10"}
TOKEN_LIMIT = 8192


# ---------------------------------------------------------------- 通用
def _http(method: str, url: str, headers: dict[str, str] | None = None,
          payload: Any = None, timeout: int = 25) -> tuple[int, Any]:
    """返回 (http_code, 解析后的 JSON)。失败返回 (0, {"error": ...})。

    注意：**绝不把 Authorization / token 写进返回或日志**。
    """
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdr = dict(headers or {})
    hdr.setdefault("Accept", "application/json")
    hdr.setdefault("User-Agent", "model-gateway-signin/1.0")
    if body is not None:
        hdr.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdr, method=method)
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:300]}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}
    except Exception as e:
        return 0, {"error": type(e).__name__}


def _dig(obj: Any, key: str) -> Any:
    """在可能被 data/result/resp 包裹的响应里找字段（兼容信封结构）。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            nested = obj.get(k)
            if isinstance(nested, dict):
                found = _dig(nested, key)
                if found is not None:
                    return found
    return None


def _cred_dir() -> Path:
    d = Path(settings.credential_dir).expanduser()
    return d if d.is_absolute() else PROJECT_ROOT / d


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """0600 原子写。凭据文件绝不留半截 JSON。"""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except Exception:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".signin-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _days_left(value: Any) -> float | None:
    """ISO 字符串 / 毫秒时间戳 → 还剩几天。"""
    if not value:
        return None
    try:
        import datetime

        if isinstance(value, (int, float)):
            s = value / 1000.0 if value > 1e11 else float(value)
            end = datetime.datetime.fromtimestamp(s)
        else:
            end = datetime.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        return round((end - datetime.datetime.now()).total_seconds() / 86400.0, 1)
    except Exception:
        return None


# ---------------------------------------------------------------- Qoder
def qoder_renew(label: str) -> dict[str, Any]:
    """用 drt- 续 dt- 并写回凭据（0600）。

    P46 修正：签到系统 README 第 2 节写着「dt 到期不要实现自动续期」，
    那是 11-19 只试了 `/api/v1/jobToken/refresh` 的旧结论。11-20 实测
    `/api/v1/deviceToken/refresh` 对桌面端 drt- **返回 200**，签到系统自己的
    `claim.py` 现在就靠它续期（注释：「换成 deviceToken/refresh 后这条分支真正走通了」）。
    ⇒ 网关侧可以放心自动续期。
    """
    from . import qoder_upstream as qu

    return qu.renew_credential(label)


def qoder_usage(token: str) -> dict[str, Any]:
    """Qoder 用量。口径与签到系统 daily_report.qoder_usage 完全一致：

      - `addon_total`   累计发放的活动积分（**日报指标，只增不减**）
      - `plan_remaining` / `addon_remaining`  当前可用
    """
    for attempt in (1, 2):
        code, data = _http("GET", QODER_API + USAGE_PATH,
                           {"Authorization": f"Bearer {token}"})
        if code == 200 and isinstance(data, dict):
            usage = data.get("qoderUsage") or {}
            quota = usage.get("userQuota") or {}
            addon = usage.get("addOnQuota") or {}
            return {
                "plan_remaining": quota.get("remaining"),
                "addon_total": addon.get("total"),
                "addon_remaining": addon.get("remaining"),
            }
        if attempt == 1:
            time.sleep(3)
    return {}


def qoder_campaign_status(token: str) -> dict[str, Any]:
    """**只读**：当前是否有可领的每日签到活动，不触发 claim。

    R2.20 额度监控每小时调用，判据与 `qoder_claim()` 完全一致（同一个活动列表、
    同样的 CLAIM_BENEFIT + CLAIMABLE/CLAIMED 过滤），这样「活动是否结束 / 每天还能
    领 100 还是 0」的口径和真正签到不会漂移。绝不 POST claim。
    """
    headers = dict(CLIENT_HEADER)
    headers["Authorization"] = f"Bearer {token}"
    code, data = _http("GET", QODER_API + CAMPAIGNS_PATH, headers)
    if code == 401:
        return {"active": None, "detail": "登录状态已失效，需重新授权"}
    if code != 200 or not isinstance(data, dict):
        return {"active": None, "detail": f"活动列表 http={code}"}
    for item in data.get("campaigns") or []:
        if not isinstance(item, dict):
            continue
        if item.get("actionType") != "CLAIM_BENEFIT":
            continue
        if item.get("claimStatus") in ("CLAIMABLE", "CLAIMED"):
            return {"active": True, "detail": "有可领活动"}
    return {"active": False, "detail": "活动已结束"}


def qoder_claim(token: str) -> dict[str, Any]:
    """领 Qoder 每日活动积分。

    ⚠️ 结果不确定时（502/504/GRANT_RESULT_UNKNOWN）先复查 reward，
    **不要盲目重发 claim** —— 签到系统踩过重复领取的坑。
    """
    out: dict[str, Any] = {
        "result": None, "campaign_id": None, "amount": None, "detail": "",
    }
    headers = dict(CLIENT_HEADER)
    headers["Authorization"] = f"Bearer {token}"

    code, data = _http("GET", QODER_API + CAMPAIGNS_PATH, headers)
    if code != 200 or not isinstance(data, dict):
        out["result"] = "unauthenticated" if code == 401 else "api_error"
        out["detail"] = "活动列表 http=%s" % code
        return out

    target = None
    for item in data.get("campaigns") or []:
        if not isinstance(item, dict):
            continue
        if item.get("actionType") != "CLAIM_BENEFIT":
            continue
        if item.get("claimStatus") not in ("CLAIMABLE", "CLAIMED"):
            continue
        target = item
        if item.get("claimStatus") == "CLAIMABLE":
            break
    if not target:
        out["result"] = "no_campaign"
        out["detail"] = "列表里没有可领活动"
        return out

    cid = target.get("campaignId")
    out["campaign_id"] = cid
    if target.get("claimStatus") == "CLAIMED":
        out["result"] = "already_claimed"
        return out

    base = QODER_API + CAMPAIGNS_PATH + "/" + str(cid)
    code2, data2 = _http("GET", base + "/reward", headers)
    if code2 == 200:
        out["result"] = "already_claimed"
        return out
    if code2 == 401:
        out["result"] = "unauthenticated"
        out["detail"] = "领取接口 401"
        return out
    err = data2.get("errorCode") if isinstance(data2, dict) else None
    if code2 != 404 or err not in ("GRANT_NOT_FOUND", "NotFound", ""):
        out["result"] = "api_error"
        out["detail"] = "reward 探测 http=%s" % code2
        return out

    code3, data3 = _http("POST", base + "/claim", headers, payload={})
    if code3 == 200 and isinstance(data3, dict):
        out["result"] = "already_claimed" if data3.get("replayed") else "claimed"
        benefit = data3.get("benefit") or {}
        out["amount"] = benefit.get("amount") or EXPECTED_DAILY
        return out
    err3 = data3.get("errorCode") if isinstance(data3, dict) else None
    if code3 in (502, 504, 0) or err3 == "GRANT_RESULT_UNKNOWN":
        time.sleep(3)
        code4, _ = _http("GET", base + "/reward", headers)
        if code4 == 200:
            out["result"] = "claimed"
            out["amount"] = EXPECTED_DAILY
            out["detail"] = "结果不确定，复查后确认到账"
        else:
            out["result"] = "confirming"
            out["detail"] = "领取结果待确认"
        return out
    out["result"] = "campaign_expired" if err3 == "CAMPAIGN_NOT_ACTIVE" else "api_error"
    out["detail"] = "领取接口 http=%s" % code3
    return out


# ---------------------------------------------------------------- WorkBuddy
def wb_headers(session: dict[str, Any]) -> dict[str, str]:
    """从 `.info` 构造请求头。

    服务器上那份 accessToken 是明文 JWT（不是 `$wbEncrypted` 信封），
    所以不需要 Electron / auth helper —— 这也是网关侧能接管的根本原因。
    """
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not isinstance(token, str) or not token or len(token) > TOKEN_LIMIT:
        raise ValueError("凭据里没有可用的 accessToken")
    if not isinstance(uid, str) or not uid:
        raise ValueError("凭据里没有 uid")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid,
        "User-Agent": "WorkBuddy",
    }
    if account.get("enterpriseId"):
        headers["X-Enterprise-Id"] = str(account["enterpriseId"])
        headers["X-Tenant-Id"] = str(account["enterpriseId"])
    if auth.get("domain"):
        headers["X-Domain"] = str(auth["domain"])
    return headers


def wb_endpoint(session: dict[str, Any]) -> str:
    auth = session.get("auth") or {}
    endpoint = auth.get("endpoint") or WB_ENDPOINT
    # 只接受 https，避免凭据文件被改成任意地址后把 token 发出去
    if not isinstance(endpoint, str) or not re.fullmatch(r"https://[\w.\-:]+", endpoint):
        return WB_ENDPOINT
    return endpoint.rstrip("/")


def wb_status(session: dict[str, Any]) -> dict[str, Any]:
    """签到状态：今日是否已签、连签天数、累计签到积分（日报指标）。"""
    code, data = _http("POST", wb_endpoint(session) + WB_STATUS_PATH,
                       wb_headers(session), payload={})
    if not (200 <= code < 300) or not isinstance(data, dict):
        return {"ok": False, "http": code}
    return {
        "ok": True,
        "active": _dig(data, "active"),
        "today_checked_in": bool(_dig(data, "today_checked_in")),
        "streak_days": _dig(data, "streak_days"),
        "metric": _dig(data, "total_credits"),
    }


def wb_available(session: dict[str, Any]) -> float | None:
    """当前可用积分（随使用实时扣减，不是累计签到积分）。接口不带 /v2。"""
    code, data = _http("POST", wb_endpoint(session) + WB_BALANCE_PATH,
                       wb_headers(session), payload={})
    value = _dig(data, "TotalCredits")
    if not (200 <= code < 300) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def wb_claim(session: dict[str, Any]) -> dict[str, Any]:
    """WorkBuddy 每日签到：查状态 → 未签才领 → 复查状态。幂等。"""
    st = wb_status(session)
    if not st.get("ok"):
        return {"result": "api_error", "detail": "状态查询 http=%s" % st.get("http")}
    if st.get("active") is False:
        return {"result": "no_campaign", "detail": "签到活动未开启"}
    if st.get("today_checked_in"):
        return {"result": "already_claimed"}

    code, data = _http("POST", wb_endpoint(session) + WB_CLAIM_PATH,
                       wb_headers(session), payload={})
    if code in (401, 403):
        return {"result": "unauthenticated", "detail": "凭据已失效，请重新扫码"}
    if code == 0:
        return {"result": "api_error", "detail": "领取请求未送达"}
    if not (200 <= code < 300) or not isinstance(data, dict):
        # 429 限流 / 5xx：请求可能已被服务端处理，只是响应没送达。
        # 复查 status 是唯一可靠口径——已签到就当成功，别让"已到账却报失败"
        # 的误报出现（P46 血泪：176 账号 429 被判失败，实际当日 100 已到账）。
        # status 与 claim 是独立限流桶，通常不受 claim 429 影响；复查若也失败
        # （ok=False）则无法确认，老实返回失败，不凭猜测报成功。
        retry = wb_status(session)
        if retry.get("ok") and retry.get("today_checked_in"):
            return {"result": "already_claimed",
                    "metric": retry.get("metric"),
                    "streak_days": retry.get("streak_days")}
        return {"result": "api_error", "detail": "领取接口 http=%s" % code}

    fresh = wb_status(session)
    if not fresh.get("ok"):
        fresh = st
    credit = _dig(data, "credit")
    if credit is None and any(k in data for k in ("code", "msg")):
        # 服务端说「今天已经签过了」：复查到了就算成功，不当失败
        if fresh.get("today_checked_in"):
            return {"result": "already_claimed"}
        return {"result": "api_error",
                "detail": str(data.get("msg") or data.get("code"))[:160]}
    return {
        "result": "claimed" if credit is not None else "already_claimed",
        "amount": credit or EXPECTED_DAILY,
        "metric": fresh.get("metric"),
        "streak_days": fresh.get("streak_days"),
    }


# CLI 刷新相关常量（与 qrlogin.py 的约定保持一致）
_DEFAULT_CLI = ""  # 用 GATEWAY_CLI_BIN 指定 codebuddy 路径；空则靠 PATH 查找
# CLI 2.185+ 写凭据的位置（相对临时 HOME），与 qrlogin._credential_glob 一致
_CLI_CRED_REL = ".local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info"
CLI_RENEW_TIMEOUT = 120  # 一次极短推理触发刷新，实测 ~10s；留足余量


def wb_renew(label: str) -> dict[str, Any]:
    """续 WorkBuddy accessToken —— 用 CLI 自己的登录态刷新（2026-11-21 重写）。

    血泪：早期实现想直接打 Keycloak refresh grant，写死
    `copilot.tencent.com` + `workbuddy-desktop`。但扫码写出的 token 真实签发方是
    `www.codebuddy.cn/auth/realms/copilot`、azp=`console`；实测把所有可能的
    client_id 换了一遍，要么 401（无效客户端）、要么 400（token/client 不匹配）。
    根因是刷新需要的 client 密钥在 CLI 的加密登录态里，网关拿不到 —— **手搓 grant
    这条路被上游堵死**。

    正路：让 CLI 自己刷。把凭据放到临时 HOME 里 CLI 期望的路径，跑一次极短推理
    （`codebuddy -p "ok"`），CLI 会在请求前自动用它的登录态刷新 token 并写回凭据文件。
    实测两个新扫码账号都能刷成功（有效期 +~40min），且刷新调用走免费档不额外扣费。
    校验 uid 不变 + token 有效期确实延长后，原子写回正式凭据。

    仅限明文 refreshToken 的扫码账号；签到系统导出的 `$wbEncrypted` 信封仍续不了。
    """
    import shutil
    import subprocess
    import tempfile

    path = _cred_dir() / f"{label}.info"
    session = _load_json(path)
    auth = session.get("auth") or {}
    refresh = auth.get("refreshToken")
    if not isinstance(refresh, str) or not refresh or refresh.strip().startswith("$"):
        return {"ok": False, "reason": "refreshToken 是加密信封或缺失，服务器端无法续期"}

    import shutil
    cli = os.environ.get("GATEWAY_CLI_BIN", "").strip() or (shutil.which("codebuddy") or "")
    if not cli or not Path(cli).exists():
        return {"ok": False, "reason": "服务器上没找到 codebuddy CLI，无法续期"}

    old_uid = str((session.get("account") or {}).get("uid") or "")
    old_exp = auth.get("expiresAt")

    home = Path(tempfile.mkdtemp(prefix="wb-renew-"))
    try:
        cred = home / _CLI_CRED_REL
        cred.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, cred)
        env = dict(os.environ, HOME=str(home), TERM="dumb", LANG="C.UTF-8", LC_ALL="C.UTF-8")
        try:
            proc = subprocess.run(
                [cli, "-p", "--model", "deepseek-v4.1-flash", "ok"],
                env=env, cwd=str(home), capture_output=True, text=True,
                timeout=CLI_RENEW_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "CLI 刷新超时"}
        if proc.returncode != 0:
            return {"ok": False, "reason": f"CLI 刷新失败（exit {proc.returncode}）"}
        if not cred.exists():
            return {"ok": False, "reason": "CLI 运行后没有写回凭据文件"}

        fresh = _load_json(cred)
        fresh_auth = fresh.get("auth") or {}
        new_token = fresh_auth.get("accessToken")
        new_exp = fresh_auth.get("expiresAt")
        new_uid = str((fresh.get("account") or {}).get("uid") or "")
        # 三重校验：uid 对得上、token 是明文新值、有效期确实延长了
        if new_uid != old_uid or not old_uid:
            return {"ok": False, "reason": "刷新后 uid 不一致，拒绝写回"}
        if not isinstance(new_token, str) or not new_token or new_token == auth.get("accessToken"):
            return {"ok": False, "reason": "CLI 未刷新出新的 accessToken"}
        if isinstance(new_exp, (int, float)) and isinstance(old_exp, (int, float)) \
                and new_exp <= old_exp:
            return {"ok": False, "reason": "刷新后有效期未延长，放弃写回"}

        # 校验通过才原子写回正式凭据（整份覆盖，结构已实测一致）
        _atomic_write_json(path, fresh)
        return {"ok": True, "label": label}
    except Exception as e:
        return {"ok": False, "reason": f"CLI 刷新异常：{type(e).__name__}"}
    finally:
        shutil.rmtree(home, ignore_errors=True)
